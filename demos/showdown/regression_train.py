import os
import jax
import pickle
import distrax
import numpy as np
import jax.numpy as jnp
import flax.linen as nn
from functools import partial
from typing import Callable
from bayes_opt import BayesianOptimization
from jax.flatten_util import ravel_pytree

from rebayes.low_rank_filter import lrvga, lofi

import hparam_tune_ekf as hp_ekf
import hparam_tune_lofi as hp_lofi


class MLP(nn.Module):
    n_out: int
    activation: Callable = nn.elu
    
    @nn.compact
    def __call__(self, x):
        x = nn.Dense(50)(x)
        x = self.activation(x)
        x = nn.Dense(self.n_out)(x)
        return x


def train_callback(bel, *args, **kwargs):
    X_test, y_test = kwargs["X_test"], kwargs["y_test"]
    apply_fn = kwargs["apply_fn"]

    yhat = apply_fn(bel.mean, X_test).squeeze()
    err = jnp.abs(y_test - yhat.ravel())
    
    res = {
        "test": err.mean(),
    }
    return res


def eval_callback_main(bel, pred, t, X, y, bel_pred, ymean, ystd, **kwargs):
    X_test, y_test = kwargs["X_test"], kwargs["y_test"]
    apply_fn = kwargs["apply_fn"]    

    yhat_test = apply_fn(bel.mean, X_test).squeeze()
    
    # Compute errors
    y_test = y_test * ystd + ymean
    yhat_test = yhat_test.ravel() * ystd + ymean
    
    y_next = y.ravel() * ystd + ymean
    yhat_next = pred.ravel() * ystd + ymean
    
    err_test = jnp.abs(y_test - yhat_test)
    err = jnp.abs(y_next - yhat_next).sum()
    
    
    res = {
        "test": err_test.mean(),
        "osa-error": err,
    }
    return res


def prepare_dataset(train, test, n_warmup=1000, n_test_warmup=300, normalise_features=True, normalise_target=True):
    data, csts = datasets.showdown_preprocess(train, test, n_warmup=n_warmup, n_test_warmup=n_test_warmup,
                                            normalise_features=normalise_features, normalise_target=normalise_target)
    data = jax.tree_map(jnp.nan_to_num, data)

    ymean = csts["ymean"]
    ystd = csts["ystd"]

    warmup_train = data["warmup_train"]
    warmup_test = data["warmup_test"]
    X_learn, y_learn = data["train"]
    X_test, y_test = data["test"]


    data = {
        "train": (X_learn, y_learn),
        "test": (X_test, y_test),
        "warmup": warmup_test,
        "ymean": ymean,
        "ystd": ystd,
    }
    return data


def tree_to_cpu(tree):
    return jax.tree_map(np.array, tree)


def get_subtree(tree, key):
    return jax.tree_map(lambda x: x[key], tree, is_leaf=lambda x: key in x)


def fwd_link_main(mean, bel, x, model, reconstruct_fn):
    params = reconstruct_fn(mean)
    means = model.apply(params, x).ravel()
    std = bel.sigma
    return means, std ** 2


def log_prob_main(mean, bel, x, y, fwd_link):
    yhat, std = fwd_link(mean, bel, x)
    std = jnp.sqrt(std)
    
    logp = distrax.Normal(yhat, std).log_prob(y).sum()
    return logp


def rmae_callback(bel, *args, **kwargs):
    X_test, y_test = kwargs["X_test"], kwargs["y_test"]
    apply_fn = kwargs["apply_fn"]
    
    yhat = apply_fn(bel.mean, X_test).squeeze()
    err = jnp.abs(y_test - yhat.ravel())
    
    res = {
        "test": err.mean(),
    }
    return res


def train_ekf_agent(params, model, method, datasets,
                    pbounds, train_callback, eval_callback,
                    optimizer_eval_kwargs,
                    random_state=314):
    train, test, warmup = datasets
    flat_params, _ = ravel_pytree(params)
    n_params = len(flat_params)

    X_train, y_train = train
    X_test, y_test = test

    optimizer, apply_fn, _ = hp_ekf.create_optimizer(
        model, pbounds, random_state, warmup, test, train_callback, method=method
    )

    optimizer.maximize(
        **optimizer_eval_kwargs,
    )

    test_kwargs = {"X_test": X_test, "y_test": y_test, "apply_fn": apply_fn}
    hparams = hp_ekf.get_best_params(n_params, optimizer, method=method)
    agent = hp_ekf.build_estimator(flat_params, hparams, None, apply_fn, method=method)
    bel, output = agent.scan(
        X_train, y_train, callback=eval_callback, progress_bar=False, **test_kwargs
    )

    res = {
        "method": method,
        "hparams": optimizer.max,
        "output": output,
        "beliefs": bel,
    }

    return res, apply_fn


def train_lofi_agent(params, params_lofi, model, method, dataset,
                     pbounds, train_callback, eval_callback,
                     optimizer_eval_kwargs,
                     random_state=314):
    train, test, warmup = dataset
    flat_params, _ = ravel_pytree(params)
    n_params = len(flat_params)

    X_train, y_train = train
    X_test, y_test = test

    optimizer, apply_fn, _ = hp_lofi.create_optimizer(
        model, pbounds, random_state, warmup, test, params_lofi, train_callback, method=method
    )

    optimizer.maximize(
        **optimizer_eval_kwargs
    )

    test_kwargs = {"X_test": X_test, "y_test": y_test, "apply_fn": apply_fn}
    hparams = hp_lofi.get_best_params(n_params, optimizer)
    agent = hp_lofi.build_estimator(flat_params, hparams, params_lofi, apply_fn, method=method)
    bel, output = agent.scan(
        X_train, y_train, callback=eval_callback, progress_bar=False, **test_kwargs
    )

    res = {
        "method": method,
        "hparams": optimizer.max,
        "output": output,
        "beliefs": bel,
    }

    return res, apply_fn


def train_lrvga_agent(key, apply_fn, model, dataset, dim_rank, n_inner, n_outer,
                      pbounds, eval_callback, fwd_link,
                      optimizer_eval_kwargs, random_state=314):
    method = "lrvga"
    train, test, warmup = dataset

    X_train, y_train = train
    X_test, y_test = test

    log_prob = partial(log_prob_main, fwd_link=fwd_link)
    def bbf(std, sigma2, eps, train, test, n_inner, n_outer):
        X_train, y_train = train
        X_test, y_test = test
        
        std = np.exp(std)
        sigma2 = np.exp(sigma2)
        eps = np.exp(eps)
            
        hparams = {
            "std": std,
            "sigma2": sigma2,
            "eps": eps,
        }
        
        bel_init, _ = lrvga.init_lrvga(key, model, X_train, dim_rank, **hparams)
        agent = lrvga.LRVGA(fwd_link, log_prob, n_samples=30, n_outer=n_outer, n_inner=n_inner)
        bel, _ = agent.scan(X_train, y_train, progress_bar=False, bel=bel_init)
        
        metric = jnp.abs(agent.predict_obs(bel, X_test) - y_test).mean()
        isna = np.isnan(metric)
        metric = 1000 if isna else metric
        return -metric
    
    pbbf = partial(bbf, train=warmup, test=test, n_outer=n_outer, n_inner=n_inner)

    optimizer = BayesianOptimization(
        f=pbbf,
        pbounds=pbounds,
        random_state=random_state,
        allow_duplicate_points=True,
    )

    optimizer.maximize(
        **optimizer_eval_kwargs
    )

    hparams = jax.tree_map(np.exp, optimizer.max["params"])
    bel_init, _ = lrvga.init_lrvga(key, model, X_train, dim_rank, **hparams)

    test_kwargs = {"X_test": X_test, "y_test": y_test, "apply_fn": apply_fn}
    agent = lrvga.LRVGA(fwd_link, log_prob, n_samples=30, n_outer=n_outer, n_inner=n_inner)
    bel, output = agent.scan(
        X_train, y_train, callback=eval_callback, progress_bar=False, bel=bel_init, **test_kwargs
    )

    res = {
        "method": method,
        "hparams": optimizer.max,
        "output": output,
        "beliefs": bel,
    }

    return res


def store_results(results, name, path):
    path = os.path.join(path, name)
    filename = f"{path}.pkl"
    with open(filename, "wb") as f:
        pickle.dump(results, f)


# TODO: remove
def load_rebayes_uci(dataset_name):
    dataset_name = "kin8nm"
    train, test = uci_regression_data.load_uci_kin8nm()
    dataset = prepare_dataset(train, test)

    ymean, ystd = dataset["ymean"], dataset["ystd"]
    dataset = dataset["train"], dataset["test"], dataset["warmup"]
    norm_factors = (ymean, ystd)

    return dataset, norm_factors


def train_agents(key, path, ix):
    res = uci_uncertainty_data.load_data(path, ix)
    dataset = res["dataset"]
    X_train, _ = dataset["train"]

    # Train, test, warmup
    dataset = dataset["train"], dataset["test"], dataset["train"][:500]

    ymean = res["ymean"]
    ystd = res["ystd"]
    eval_callback = partial(eval_callback_main, ymean=ymean, ystd=ystd)

    _, dim_in = X_train.shape
    dim_out = 1
    model = MLP(dim_out, activation=nn.relu)
    params = model.init(key, jnp.ones((1, dim_in)))
    params_flat, reconstruct_fn =  ravel_pytree(params)

    optimizer_eval_kwargs = {
        "init_points": 10,
        "n_iter": 15,
    }

    pbounds = {
        "log_init_cov": (-5, 0.0),
        "dynamics_weights": (0, 1.0),
        "log_emission_cov": (-7, 0.0),
        "dynamics_log_cov": (-7, 0.0),
    }

    pbounds_lofi = pbounds.copy()
    pbounds_lofi.pop("dynamics_log_cov")
    pbounds_lofi["dynamics_covariance"] = None

    pbounds_lrvga = {
        "std": (-0.34, 0.0),
        "sigma2": (-4, 0.0),
        "eps": (-10, -4),
    }

    # -------------------------------------------------------------------------
    # Extended Kalman Filter
    methods = ["fdekf", "vdekf", "fcekf"]
    for method in methods:
        res, apply_fn = train_ekf_agent(
            params, model, method, dataset, pbounds,
            train_callback, eval_callback,
            optimizer_eval_kwargs,
        )

        metric_final = res["output"]["test"][-1]
        print(method)
        print(f"{metric_final:=0.4f}")
        print("-" * 80)
        store_results(res, f"{dataset_name}_{method}_{ix}", output_path)

    # -------------------------------------------------------------------------
    # Low-rank filter
    methods = ["full_svd_lofi", "orth_svd_lofi"]
    params_lofi = lofi.LoFiParams(
        memory_size=dim_rank,
        sv_threshold=0,
        steady_state=True,
    )
    for method in methods:
        res, apply_fn = train_lofi_agent(
            params, params_lofi, model, method, dataset, pbounds_lofi,
            train_callback, eval_callback,
            optimizer_eval_kwargs,
        )

        metric_final = res["output"]["test"][-1]
        print(method)
        print(f"{metric_final:=0.4f}")
        print("-" * 80)
        store_results(res, f"{dataset_name}_{method}_{ix}", output_path)


    # -------------------------------------------------------------------------
    # Low-rank variational Gaussian approximation (LRVGA)
    n_outer = 6
    n_inner = 4
    fwd_link = partial(fwd_link_main, model=model, reconstruct_fn=reconstruct_fn)
    res = train_lrvga_agent(
        key, apply_fn, model, dataset, dim_rank=dim_rank, n_inner=n_inner, n_outer=n_outer,
        pbounds=pbounds_lrvga, eval_callback=eval_callback,  fwd_link=fwd_link,
        optimizer_eval_kwargs=optimizer_eval_kwargs,
        random_state=random_state,
    ) 
    method = res["method"]

    metric_final = res["output"]["test"][-1]
    print(method)
    print(f"{metric_final:=0.4f}")
    print("-" * 80)
    store_results(res, f"{dataset_name}_{method}_{ix}", output_path)


if __name__ == "__main__":
    import sys
    from itertools import product
    from rebayes.utils import uci_regression_data, datasets, uci_uncertainty_data
    # TOODO: change to $REBAYES_OUTPUT
    output_path = "/home/gerardoduran/documents/rebayes/demos/outputs/checkpoints"

    # _, dataset_name = sys.argv
    
    dim_rank = 50
    random_state = 314
    key = jax.random.PRNGKey(314)

    num_partitions = 20
    partitions = range(num_partitions)
    datasets = [
        "bostonHousing", "concrete", "energy", "kin8nm", "naval-propulsion-plant",
        "power-plant", "protein-tertiary-structure", "wine-quality-red", "yacht"
    ]

    for i, (dataset_name, ix) in enumerate(product(datasets, partitions)):
        keyv = jax.random.fold_in(key, i)
        print(f"Fitting {dataset_name} --- {ix}")
        path = (
            "/home/gerardoduran/documents/external"
            "/DropoutUncertaintyExps/UCI_Datasets/"
            f"{dataset_name}"
        )
        path = os.path.join(path, "data")
        train_agents(keyv, path, ix)
        print("\n" * 3)