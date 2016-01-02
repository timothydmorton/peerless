#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import h5py
import emcee
import logging
import traceback
import numpy as np
import pandas as pd
from scipy.stats import beta
from functools import partial
# import matplotlib.pyplot as pl
from multiprocessing import Pool

import george
from george import kernels

import transit

from peerless.data import load_light_curves_for_kic
from peerless.catalogs import KICatalog


# Newton's constant in $R_\odot^3 M_\odot^{-1} {days}^{-2}$.
_G = 2945.4625385377644


class TransitModel(object):

    eb = beta(1.12, 3.09)

    def __init__(self, gps, system, smass, smass_err, srad, srad_err,
                 fit_lcs, other_lcs):
        self.gps = gps
        self.system = system
        self.smass = smass
        self.smass_err = smass_err
        self.srad = srad
        self.srad_err = srad_err
        self.fit_lcs = fit_lcs
        self.other_lcs = other_lcs

    # Probabilistic model:
    def lnprior(self):
        star = self.system.central
        return -0.5 * (
            ((star.mass - self.smass) / self.smass_err) ** 2 +
            ((star.radius - self.srad) / self.srad_err) ** 2
        ) + self.eb.logpdf(self.system.bodies[0].e)

    def lnlike(self):
        system = self.system
        ll = 0.0
        preds = []
        for gp, lc in zip(self.gps, self.fit_lcs):
            mu = system.light_curve(lc.time, texp=lc.texp)
            r = lc.flux - mu
            ll += gp.lnlikelihood(r, quiet=True)
            if not np.isfinite(ll):
                return -np.inf, (0, None)
            preds.append((gp.predict(lc.flux, lc.time, return_cov=False)+mu,
                          mu))

        # Compute number of cadences with transits in the other light curves.
        ncad = sum((system.light_curve(lc.time) < system.central.flux).sum()
                   for lc in self.other_lcs)

        return ll, (ncad, preds)

    def lnprob(self, theta):
        blob = [None, 0, None]
        try:
            self.system.set_vector(theta[:len(self.system)])
        except ValueError:
            return -np.inf, blob
        blob[0] = (
            self.system.bodies[0].period,
            self.system.bodies[0].e,
            self.system.bodies[0].b,
        )

        i = len(self.system)
        for gp in self.gps:
            n = len(gp)
            gp.set_vector(theta[i:i+n])
            i += n

        lp = self.lnprior()
        if not np.isfinite(lp):
            return -np.inf, blob

        ll, (blob[1], blob[2]) = self.lnlike()
        if not np.isfinite(ll):
            return -np.inf, blob

        return lp + ll, blob

    def __call__(self, theta):
        return self.lnprob(theta)


def fit_light_curve(args, remove_kois=False, output_dir="fits", plot_all=False,
                    no_plots=False, verbose=False, quiet=False, delete=False,
                    nburn=200, burniter=2, nsteps=1000):
    kicid = args["kicid"]

    # Initialize the system.
    system = transit.System(transit.Central(
        flux=1.0, radius=args["srad"], mass=args["smass"], q1=0.5, q2=0.5,
    ))
    system.add_body(transit.Body(
        radius=args["radius"],
        period=args["period"],
        t0=args["t0"],
        b=args["impact"],
        e=0.01,
        omega=0.0,
    ))
    system.thaw_parameter("*")
    system.freeze_parameter("bodies*ln_mass")

    # Load the light curves.
    lcs, _ = load_light_curves_for_kic(kicid, delete=delete,
                                       remove_kois=remove_kois)

    # Which light curves should be fit?
    fit_lcs = []
    other_lcs = []
    gps = []
    for lc in lcs:
        f = system.light_curve(lc.time, lc.texp)
        if np.any(f < 1.0):
            fit_lcs.append(lc)
            kernel = np.var(lc.flux) * kernels.Matern32Kernel(2**2)
            gp = george.GP(kernel, white_noise=2*np.log(np.mean(lc.ferr)),
                           fit_white_noise=True)
            gp.compute(lc.time, lc.ferr)
            gps.append(gp)
        else:
            other_lcs.append(lc)

    model = TransitModel(gps, system, args["smass"], args["smass_err"],
                         args["srad"], args["srad_err"], fit_lcs, other_lcs)

    cols = system.get_parameter_names()
    for i, g in enumerate(gps):
        cols += list(map("gp[{0}].{{0}}".format(i).format,
                         g.get_parameter_names()))
    p0 = np.concatenate([system.get_vector()]+[g.get_vector() for g in gps])
    ndim, nwalkers = len(p0), 42
    sampler = emcee.EnsembleSampler(nwalkers, ndim, model)

    # Set up the output.
    basedir = os.path.join(output_dir, "{0}".format(kicid))
    os.makedirs(basedir, exist_ok=True)
    chain_fn = os.path.join(basedir, "chain.h5")
    with h5py.File(chain_fn, "w") as f:
        g = f.create_dataset("chain", shape=(nsteps, nwalkers),
                             dtype=[(k, np.float64) for k in cols])
        f.create_dataset("lnprob", shape=(nsteps, nwalkers))
        f.create_dataset("params", shape=(nsteps, nwalkers),
                         dtype=[("ncadence", np.int64),
                                ("period", np.float64),
                                ("impact", np.float64),
                                ("eccen", np.float64)])

        n = sum(len(lc.time) for lc in fit_lcs)
        f.create_dataset("pred", shape=(nsteps, nwalkers, 2, n))

        data = np.array([
            (i, lc.time[j], lc.flux[j])
            for i, lc in enumerate(lcs) for j in range(len(lc.time))
        ], dtype=[("chunk", np.int64), ("time", np.float64),
                  ("flux", np.float64)])
        f.create_dataset("data", data=data)

    # Run the MCMC.
    burniter = max(1, burniter)
    for i in range(burniter):
        print("Burn-in: {0}".format(i))
        p0 = p0[None, :] + 1e-4 * np.random.randn(nwalkers, ndim)
        p0, _, _, _ = sampler.run_mcmc(p0, nburn)
        if i < burniter - 1:
            p0 = sampler.flatchain[np.argmax(sampler.flatlnprobability)]

    print("Production")
    sampler.reset()
    for i, (pos, lnp, _, blob) in enumerate(sampler.sample(p0,
                                                           iterations=nsteps,
                                                           storechain=False)):
        with h5py.File(chain_fn, "a") as f:
            for n in range(nwalkers):
                for j, k in enumerate(cols):
                    f["chain"][i, n, k] = pos[n, j]

                f["params"][i, n, "ncadence"] = blob[n][1]
                f["params"][i, n, "period"] = blob[n][0][0]
                f["params"][i, n, "eccen"] = blob[n][0][1]
                f["params"][i, n, "impact"] = blob[n][0][2]

                f["pred"][i, n, 0] = np.concatenate([b[0] for b in blob[n][2]])
                f["pred"][i, n, 1] = np.concatenate([b[1] for b in blob[n][2]])

            f["lnprob"][i] = lnp

    return sampler


def _wrapper(*args, **kwargs):
    quiet = kwargs.pop("quiet", False)
    try:
        return fit_light_curve(*args, **kwargs)
    except:
        if not quiet:
            raise
        with open(os.path.join(kwargs.get("output_dir", "fits"),
                               "errors.txt"), "a") as f:
            f.write("{0} failed with exception:\n{1}"
                    .format(args, traceback.format_exc()))
    return [], None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="model some light curves")

    parser.add_argument("kicids", nargs="*", type=int, help="some KIC IDs")
    parser.add_argument("--candidates",
                        help="the ")
    parser.add_argument("-p", "--parallel", type=int,
                        help="parallelize across targets")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="log errors instead of raising")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="more output to the screen")
    parser.add_argument("-c", "--clean", action="store_true",
                        help="remove temporary light curve files")
    parser.add_argument("-o", "--output-dir", default="output",
                        help="the output directory")
    parser.add_argument("--no-plots", action="store_true",
                        help="don't make any plots")

    parser.add_argument("--no-remove-kois", action="store_true",
                        help="leave the known KOIs in the light curves")
    parser.add_argument("--fit-all", action="store_true",
                        help="fit even rejected candidates")
    parser.add_argument("--max-offset", type=float, default=10.0,
                        help="the maximum centroid offset S/N")
    parser.add_argument("--nburn", type=int, default=300,
                        help="the number of burn-in MCMC steps")
    parser.add_argument("--burniter", type=int, default=3,
                        help="the number of burn-in iterations")
    parser.add_argument("--nsteps", type=int, default=5000,
                        help="the number of production MCMC steps")

    args = parser.parse_args()

    # Build the dictionary of search keywords.
    function = partial(
        _wrapper,
        remove_kois=not args.no_remove_kois,
        output_dir=args.output_dir,
        no_plots=args.no_plots,
        verbose=args.verbose,
        quiet=args.quiet,
        delete=args.clean,
        nburn=args.nburn,
        burniter=args.burniter,
        nsteps=args.nsteps,
    )

    # Check and create the output directory.
    if os.path.exists(args.output_dir):
        logging.warning("Output directory '{0}' exists"
                        .format(args.output_dir))
    else:
        os.makedirs(args.output_dir)

    # Load the candidate list.
    cands = pd.read_csv(args.candidates)
    if len(args.kicids):
        cands = cands[cands.kicid.isin(args.kicids)]
    if not args.fit_all:
        cands = cands[cands.accept_time & cands.accept_bic]
    if not args.fit_all:
        cands = cands[cands.accept_time & cands.accept_bic]
    cands["centroid_offset_s2n"] = \
        cands.centroid_offset / cands.centroid_offset_err

    # Load the stellar catalog.
    kic = KICatalog().df
    kic = kic[kic.kepid.isin(cands.kicid)]

    # Initialize.
    inits = []
    for id_, rows in cands.groupby("kicid"):
        if not np.any(rows.centroid_offset_s2n <= args.max_offset):
            continue

        # Save the stellar parameters.
        star = kic[kic.kepid == id_]
        system = dict(
            kicid=id_,
            srad=float(star.radius),
            srad_err=0.5 * float(star.radius_err1 - star.radius_err2),
            smass=float(star.mass),
            smass_err=0.5 * float(star.mass_err1 - star.mass_err2),
        )

        # Multiple transits.
        if len(rows) > 1:
            times = np.sort(rows.transit_time)
            system["period"] = np.mean(np.diff(times))
            system["t0"] = times[0]
            row = rows.mean()
        else:
            system["period"] = 2000.0
            system["t0"] = float(rows.transit_time)
            row = rows.iloc[0]

        # Initial parameters.
        system["radius"] = float(row.transit_ror * star.radius)

        # Semi-major.
        a = float(_G*system["period"]**2*(star.mass)/(4*np.pi*np.pi)) ** (1./3)

        # Duration.
        dur = system["period"] * system["radius"] / (np.pi * a)
        b = np.sqrt(np.abs(1 - dur / float(row.transit_duration)))
        system["impact"] = b
        inits.append(system)

    # Deal with parallelization.
    if args.parallel:
        pool = Pool(args.parallel)
        M = pool.imap_unordered
    else:
        M = map

    # Run.
    samplers = list(M(function, inits))
