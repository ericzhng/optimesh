# -*- coding: utf-8 -*-
#
"""
Optimal Delaunay Triangulation.

Long Chen, Michael Holst,
Efficient mesh optimization schemes based on Optimal Delaunay
Triangulations,
Comput. Methods Appl. Mech. Engrg. 200 (2011) 967–984,
<https://doi.org/10.1016/j.cma.2010.11.007>.
"""
from __future__ import print_function

import numpy
import fastfunc
import quadpy

from meshplex import MeshTri

from .helpers import (
    runner,
    get_new_points_volume_averaged,
    get_new_points_count_averaged,
    print_stats,
)


def energy(mesh, uniform_density=False):
    """The mesh energy is defined as

    E = int_Omega |u_l(x) - u(x)| rho(x) dx

    where u(x) = ||x||^2 and u_l is its piecewise linearization on the mesh.
    """
    # E = 1/(d+1) sum_i ||x_i||^2 |omega_i| - int_Omega_i ||x||^2
    dim = mesh.cells["nodes"].shape[1] - 1

    star_volume = numpy.zeros(mesh.node_coords.shape[0])
    for i in range(3):
        idx = mesh.cells["nodes"][:, i]
        if uniform_density:
            # rho = 1,
            # int_{star} phi_i * rho = 1/(d+1) sum_{triangles in star} |triangle|
            fastfunc.add.at(star_volume, idx, mesh.cell_volumes)
        else:
            # rho = 1 / tau_j,
            # int_{star} phi_i * rho = 1/(d+1) |num triangles in star|
            fastfunc.add.at(star_volume, idx, numpy.ones(idx.shape, dtype=float))
    x2 = numpy.einsum("ij,ij->i", mesh.node_coords, mesh.node_coords)
    out = 1 / (dim + 1) * numpy.dot(star_volume, x2)

    # could be cached
    assert dim == 2
    x = mesh.node_coords[:, :2]
    triangles = numpy.moveaxis(x[mesh.cells["nodes"]], 0, 1)
    val = quadpy.triangle.integrate(
        lambda x: x[0] ** 2 + x[1] ** 2,
        triangles,
        # Take any scheme with order 2
        quadpy.triangle.Dunavant(2),
    )
    if uniform_density:
        val = numpy.sum(val)
    else:
        rho = 1.0 / mesh.cell_volumes
        val = numpy.dot(val, rho)

    assert out >= val

    return out - val


def fixed_point_uniform(points, cells, *args, **kwargs):
    """Idea:
    Move interior mesh points into the weighted averages of the circumcenters
    of their adjacent cells. If a triangle cell switches orientation in the
    process, don't move quite so far.
    """

    def get_new_points(mesh):
        # Get circumcenters everywhere except at cells adjacent to the boundary;
        # barycenters there.
        cc = mesh.cell_circumcenters
        bc = mesh.cell_barycenters
        # Find all cells with a boundary edge
        boundary_cell_ids = mesh.edges_cells[1][:, 0]
        cc[boundary_cell_ids] = bc[boundary_cell_ids]
        return get_new_points_volume_averaged(mesh, cc)

    mesh = MeshTri(points, cells)
    runner(get_new_points, mesh, *args, **kwargs)
    return mesh.node_coords, mesh.cells["nodes"]


def fixed_point_density_preserving(points, cells, *args, **kwargs):
    """Idea:
    Move interior mesh points into the weighted averages of the circumcenters
    of their adjacent cells. If a triangle cell switches orientation in the
    process, don't move quite so far.
    """

    def get_new_points(mesh):
        # Get circumcenters everywhere except at cells adjacent to the boundary;
        # barycenters there.
        cc = mesh.cell_circumcenters
        bc = mesh.cell_barycenters
        # Find all cells with a boundary edge
        boundary_cell_ids = mesh.edges_cells[1][:, 0]
        cc[boundary_cell_ids] = bc[boundary_cell_ids]
        return get_new_points_count_averaged(mesh, cc)

    mesh = MeshTri(points, cells)
    runner(get_new_points, mesh, *args, **kwargs)
    return mesh.node_coords, mesh.cells["nodes"]


def nonlinear_optimization_uniform(
    X,
    cells,
    tol,
    max_num_steps,
    verbose=False,
    step_filename_format=None,
    callback=None,
):
    """Optimal Delaunay Triangulation smoothing.

    This method minimizes the energy

        E = int_Omega |u_l(x) - u(x)| rho(x) dx

    where u(x) = ||x||^2, u_l is its piecewise linear nodal interpolation and
    rho is the density. Since u(x) is convex, u_l >= u everywhere and

        u_l(x) = sum_i phi_i(x) u(x_i)

    where phi_i is the hat function at x_i. With rho(x)=1, this gives

        E = int_Omega sum_i phi_i(x) u(x_i) - u(x)
          = 1/(d+1) sum_i ||x_i||^2 |omega_i| - int_Omega ||x||^2

    where d is the spatial dimension and omega_i is the star of x_i (the set of
    all simplices containing x_i).
    """
    import scipy.optimize

    mesh = MeshTri(X, cells)

    if step_filename_format:
        mesh.save(
            step_filename_format.format(0),
            show_centroids=False,
            show_coedges=False,
            show_axes=False,
            nondelaunay_edge_color="k",
        )

    print("Before:")
    extra_cols = ["energy: {:.5e}".format(energy(mesh))]
    print_stats(mesh, extra_cols=extra_cols)

    def f(x):
        mesh.node_coords[mesh.is_interior_node] = x.reshape(-1, X.shape[1])
        mesh.update_values()
        return energy(mesh, uniform_density=True)

    # TODO put f and jac together
    def jac(x):
        mesh.node_coords[mesh.is_interior_node] = x.reshape(-1, X.shape[1])
        mesh.update_values()

        grad = numpy.zeros(mesh.node_coords.shape)
        cc = mesh.cell_circumcenters
        for mcn in mesh.cells["nodes"].T:
            fastfunc.add.at(
                grad, mcn, ((mesh.node_coords[mcn] - cc).T * mesh.cell_volumes).T
            )
        gdim = 2
        grad *= 2 / (gdim + 1)
        return grad[mesh.is_interior_node].flatten()

    def flip_delaunay(x):
        flip_delaunay.step += 1
        # Flip the edges
        mesh.node_coords[mesh.is_interior_node] = x.reshape(-1, X.shape[1])
        mesh.update_values()
        mesh.flip_until_delaunay()

        if step_filename_format:
            mesh.save(
                step_filename_format.format(flip_delaunay.step),
                show_centroids=False,
                show_coedges=False,
                show_axes=False,
                nondelaunay_edge_color="k",
            )
        if verbose:
            print("\nStep {}:".format(flip_delaunay.step))
            print_stats(mesh, extra_cols=["energy: {}".format(f(x))])

        if callback:
            callback(flip_delaunay.step, mesh)

        # mesh.show()
        # exit(1)
        return

    flip_delaunay.step = 0

    x0 = X[mesh.is_interior_node].flatten()

    if callback:
        callback(0, mesh)

    out = scipy.optimize.minimize(
        f,
        x0,
        jac=jac,
        # method="Nelder-Mead",
        # method="Powell",
        # method="CG",
        # method="Newton-CG",
        method="BFGS",
        # method="L-BFGS-B",
        # method="TNC",
        # method="COBYLA",
        # method="SLSQP",
        tol=tol,
        callback=flip_delaunay,
        options={"maxiter": max_num_steps},
    )
    # Don't assert out.success; max_num_steps may be reached, that's fine.

    # One last edge flip
    mesh.node_coords[mesh.is_interior_node] = out.x.reshape(-1, X.shape[1])
    mesh.update_values()
    mesh.flip_until_delaunay()

    print("\nFinal ({} steps):".format(out.nit))
    extra_cols = ["energy: {:.5e}".format(energy(mesh))]
    print_stats(mesh, extra_cols=extra_cols)
    print()

    return mesh.node_coords, mesh.cells["nodes"]
