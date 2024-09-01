from __future__ import annotations

import itertools
from typing import cast

import optuna
from optuna.samplers import BaseSampler
from optuna.samplers import CmaEsSampler
from optuna.samplers import NSGAIISampler
from optuna.samplers import RandomSampler
from optuna.samplers import TPESampler
from optuna.testing.storages import StorageSupplier


def parse_args(args: str) -> list[int | str]:
    ret: list[int | str] = []
    for arg in map(lambda s: s.strip(), args.split(",")):
        try:
            ret.append(int(arg))
        except ValueError:
            ret.append(arg)
    return ret


SAMPLER_MODES = [
    "random",
    "tpe",
    "cmaes",
]


def create_sampler(sampler_mode: str) -> BaseSampler:
    if sampler_mode == "random":
        return RandomSampler()
    elif sampler_mode == "tpe":
        return TPESampler()
    elif sampler_mode == "cmaes":
        return CmaEsSampler()
    elif sampler_mode == "nsgaii":
        return NSGAIISampler()
    else:
        assert False


class OptimizeSuite:
    def objective(self, trial: optuna.Trial, n_objectives: int) -> list[float]:
        x = trial.suggest_float("x", -100, 100)
        y = trial.suggest_int("y", -100, 100)
        objective_values = [x**2 + y**2, (x - 2) ** 2 + (y - 2) ** 2, (x + 2) ** 2 + (y + 2) ** 2]
        return objective_values[:n_objectives]

    def optimize(
        self, storage_mode: str, sampler_mode: str, n_trials: int, n_objectives: int
    ) -> None:
        with StorageSupplier(storage_mode) as storage:
            sampler = create_sampler(sampler_mode)
            assert n_objectives in [1, 2, 3]
            directions = ["minimize"] * n_objectives
            study = optuna.create_study(storage=storage, sampler=sampler, directions=directions)
            study.optimize(lambda trial: self.objective(trial, n_objectives), n_trials=n_trials)

    def time_optimize(self, args: str) -> None:
        storage_mode, sampler_mode, n_trials, n_objectives = parse_args(args)
        storage_mode = cast(str, storage_mode)
        sampler_mode = cast(str, sampler_mode)
        n_trials = cast(int, n_trials)
        n_objectives = cast(int, n_objectives)
        self.optimize(storage_mode, sampler_mode, n_trials, n_objectives)

    params = (
        "inmemory, random, 1000, 1",
        "inmemory, random, 10000, 1",
        "inmemory, tpe, 1000, 1",
        "inmemory, cmaes, 1000, 1",
        "sqlite, random, 1000, 1",
        "sqlite, tpe, 1000, 1",
        "sqlite, cmaes, 1000, 1",
        "journal, random, 1000, 1",
        "journal, tpe, 1000, 1",
        "journal, cmaes, 1000, 1",
        "inmemory, tpe, 1000, 2",
        "inmemory, nsgaii, 1000, 2",
        "sqlite, tpe, 1000, 2",
        "sqlite, nsgaii, 1000, 2",
        "journal, tpe, 1000, 2",
        "journal, nsgaii, 1000, 2",
        "inmemory, tpe, 1000, 3",
        "inmemory, nsgaii, 1000, 3",
        "sqlite, tpe, 1000, 3",
        "sqlite, nsgaii, 1000, 3",
        "journal, tpe, 1000, 3",
        "journal, nsgaii, 1000, 3",
    )

    param_names = ["storage, sampler, n_trials, n_objectives"]
    timeout = 600
