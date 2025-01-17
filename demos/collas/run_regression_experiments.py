import argparse
from functools import partial
import json
import os
from typing import Callable
from pathlib import Path
import pickle

import jax.random as jr
from jax.tree_util import tree_map

import demos.collas.datasets.dataloaders as dataloaders
import rebayes.utils.models as models
import rebayes.utils.callbacks as callbacks
import demos.collas.hparam_tune as hparam_tune
import demos.collas.train_utils as train_utils

AGENT_TYPES = ["lofi", "fdekf", "vdekf", "sgd-rb", "adam-rb"]


def _check_positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    
    return ivalue


def _check_nonneg_float(value):
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive float")
    
    return fvalue


def _compute_io_dims(dataset_type):
    input_dim, output_dim = 0, 1
    if "mnist" in dataset_type:
        input_dim = (1, 28, 28, 1)
    elif "cifar" in dataset_type:
        input_dim = (1, 32, 32, 3)
    
    return input_dim, output_dim


def _process_agent_args(agent_args, tune_sgd_momentum, ranks, input_dim, 
                        output_dim, problem, obs_scale, nll_method):
    agents = {}
    sgd_loss_fn = partial(callbacks.nll_reg, scale=obs_scale)
    
    # Bounds for tuning
    sgd_pbounds = {
        "log_learning_rate": (-10.0, 0.0),
    }
    if nll_method == "nlpd-mc":
        sgd_pbounds["log_init_cov"] = (-10.0, 0.0)
    if problem == "iid":
        filter_pbounds = {
            'log_init_cov': (-10, 0.0),
            'log_1m_dynamics_weights': (-90, -90),
            'log_dynamics_cov': (-90, -90),
            'log_alpha': (-30, 0),
        }
    else:
        filter_pbounds = {
            'log_init_cov': (-10, 0),
            'log_1m_dynamics_weights': (-30, 0),
            'log_dynamics_cov': (-30, 0),
            'log_alpha': (-30, 0),
        }
    
    # Create agents
    if "lofi" in agent_args:
        agents.update({
            f'lofi-{rank}': {
                'memory_size': rank,
                'inflation': "hybrid",
                'lofi_method': "diagonal",
                'pbounds': filter_pbounds,
            } for rank in ranks
        })
    if "fdekf" in agent_args:
        agents["fdekf"] = {'pbounds': filter_pbounds}
    if "vdekf" in agent_args:
        agents["vdekf"] = {'pbounds': filter_pbounds}
    if "sgd-rb" in agent_args:
        pbounds = sgd_pbounds.copy()
        if tune_sgd_momentum:
            pbounds["log_1m_momentum"] = (-10.0, 0.0)
        agents.update({
            f'sgd-rb-{rank}': {
                'loss_fn': sgd_loss_fn,
                'buffer_size': rank,
                'dim_input': input_dim,
                'dim_output': output_dim,
                "optimizer": "sgd",
                'pbounds': pbounds,
            } for rank in ranks
        })
    if "adam-rb" in agent_args:
        agents.update({
            f'adam-rb-{rank}': {
                'loss_fn': sgd_loss_fn,
                'buffer_size': rank,
                'dim_input': input_dim,
                'dim_output': output_dim,
                "optimizer": "adam",
                'pbounds': sgd_pbounds,
            } for rank in ranks
        })
    
    return agents


def _eval_metric(
    obs_scale: float,
    problem: str,
    nll_method: str,
    temperature: float,
    aleatoric_factor: float = 1.0,
) -> dict:
    """Get evaluation metric for classification problem type.
    """
    linearize = nll_method == "nlpd-linearized"
    if problem == "iid":
        if nll_method == "nll":
            result = {
                "val": partial(
                    callbacks.cb_eval,
                    evaluate_fn=callbacks.generate_ll_reg_eval_fn(obs_scale)
                ),
                "test": partial(
                    callbacks.cb_eval,
                    evaluate_fn=partial(callbacks.reg_eval_fn, scale=obs_scale)
                )
            }
        else: # nlpd-mc
            result = {
                "val": lambda *args, **kwargs: tree_map(
                    lambda x: -x, partial(
                        callbacks.cb_reg_nlpd_mc, temperature=temperature,
                        linearize=linearize, aleatoric_factor=aleatoric_factor,
                    )(*args, **kwargs)
                ),
                "test": partial(
                    callbacks.cb_reg_nlpd_mc, temperature=temperature,
                    linearize=linearize, aleatoric_factor=aleatoric_factor,
                ),
            }
    elif problem == "permuted":
        result = {
            "val": partial(callbacks.cb_osa,
                            evaluate_fn=partial(callbacks.ll_reg,
                                                scale=obs_scale),
                            label="log_likelihood"),
            "test": partial(callbacks.cb_reg_discrete_tasks,
                            scale=obs_scale)
        }
    else: # Non-iid rotations
        if nll_method == "nll":
            result = {
                "val": partial(callbacks.cb_osa,
                                evaluate_fn=partial(callbacks.ll_reg,
                                                    scale=obs_scale),
                                label="log_likelihood"),
                "test": partial(callbacks.cb_reg_sup,
                                ymean=0.0, ystd=1.0, 
                                only_window_eval=True)
            }
        else: # nlpd-mc
            result = {
                "val": partial(callbacks.cb_mc_osa,
                               temperature=temperature, linearize=linearize,
                               aleatoric_factor=aleatoric_factor,
                               label="log_likelihood"),
                "test": partial(callbacks.cb_reg_mc_window,
                                temperature=temperature, linearize=linearize,
                                aleatoric_factor=aleatoric_factor,)
            }
    
    return result


def tune_and_store_hyperparameters(
    hparam_path: Path,
    model_init_fn: Callable,
    dataset_load_fn: Callable,
    agents: dict,
    val_callback: Callable,
    verbose: int = 2,
    n_explore: int = 20,
    n_exploit: int = 25,
    temperature: float = 1.0,
    nll_method: str = "nll",
) -> dict:
    """Tune and store hyperparameters.

    Args:
        hparam_path (Path): Path to hyperparmeter directory.
        model_init_fn (Callable): Model initialization function.
        dataset_load_fn (Callable): Dataset loading function.
        agents (dict): Dictionary of agent parameters.
        val_callback (Callable): Tuning objective.
        verbosity (int, optional): Verbosity level for Bayesian optimization.
        n_explore (int, optional): Number of random exploration steps
            for Bayesian optimization. Defaults to 20.
        n_exploit (int, optional): Number of exploitation steps for
            Bayesian optimization. Defaults to 25.

    Returns:
        hparams (dict): Dictionary of tuned hyperparameters.
    """
    hparam_path.mkdir(parents=True, exist_ok=True)
    dataset = dataset_load_fn()
    
    hparams = {}
    for agent_name, agent_params in agents.items():
        print(f"Tuning {agent_name}...")
        pbounds = agent_params.pop("pbounds")
        optimizer = hparam_tune.create_optimizer(
            model_init_fn, pbounds, dataset["train"], dataset["val"],
            val_callback, agent_name, verbose=verbose, callback_at_end=False,
            nll_method=nll_method, classification=False, **agent_params
        )
        optimizer.maximize(init_points=n_explore, n_iter=n_exploit)
        best_hparams = hparam_tune.get_best_params(optimizer, agent_name,
                                                   nll_method=nll_method)
        # Store as json
        # agent_filepath = agent_name
        # if temperature != 1.0:
        #     agent_filepath += f"-temp-{temperature}"
        with open(Path(hparam_path, f"{agent_name}.json"), "w") as f:
            json.dump(best_hparams, f)
        hparams[agent_name] = best_hparams

    return hparams


def evaluate_and_store_result(
    output_path: Path,
    model_init_fn: Callable,
    dataset_load_fn: Callable,
    optimizer_dict: dict,
    eval_callback: Callable,
    agent_name: str,
    problem: str,
    n_iter: int=20,
    key: int=0,
    temperature: float=1.0,
    **kwargs: dict,
) -> dict:
    """Evaluate and store results.

    Args:

        model_init_fn (Callable): Model initialization function.
        dataset_load_fn (Callable): Dataset loading function.
        optimizer_dict (dict): Dictionary of optimizer parameters.
        eval_callback (Callable): Evaluation callback.
        problem (str): Problem type.
        n_iter (int, optional): Number of random initializations. Defaults to 20.
        key (int, optional): Random seed. Defaults to 0.
        temperature (float, optional): Temperature for NLPD-MC sampling.

    Returns:
        result (dict): Dictionary of results.
    """
    if isinstance(key, int):
        key = jr.PRNGKey(key)
    if problem == "iid" or problem == "amplified" or problem == "random-walk":
        eval_fn = train_utils.eval_agent_stationary
    else: # Permuted regression
        eval_fn = train_utils.eval_agent_nonstationary
    result = eval_fn(model_init_fn, dataset_load_fn, optimizer_dict,
                     eval_callback, n_iter, key, **kwargs)
    
    # Store result
    # agent_name = f"{agent_name}-temp-{temperature}"
    with open(Path(output_path, f"{agent_name}.pkl"), "wb") as f:
        pickle.dump(result, f)
    
    return result


def main(cl_args):
    # Set output path
    output_path = os.environ.get("REBAYES_OUTPUT")
    problem_str = cl_args.problem
    nll_method = cl_args.nll_method
    if output_path is None:
        output_path = Path("regression", "outputs", problem_str,
                           cl_args.dataset, cl_args.model, nll_method,)
    Path(output_path).mkdir(parents=True, exist_ok=True)
    
    # Set config path
    config_path = os.environ.get("REBAYES_CONFIG")
    if config_path is None:
        config_path = Path("regression", "configs")
    Path(config_path).mkdir(parents=True, exist_ok=True)
    
    # Load dataset
    dataset = dataloaders.reg_datasets[cl_args.problem]
    if cl_args.problem == "permuted":
        dataset = dataset()
    else:
        dataset = dataset(cl_args.ntrain)
    dataset_load_fn, kwargs = dataset.values()
    ntrain = None
    if kwargs is not None and "ntrain" in kwargs:
        ntrain = kwargs["ntrain"]
    base_dataset = dataloaders.load_target_digit_dataset(
        target_digit=cl_args.target_digit, 
        dataset_type=cl_args.dataset, n=ntrain,
    )
    dataset_load_fn = partial(dataset_load_fn, dataset=base_dataset)
    eval_metric = _eval_metric(cl_args.obs_scale, cl_args.problem, 
                               cl_args.nll_method, cl_args.temp,
                               cl_args.aleatoric)
    
    # Initialize model
    if cl_args.model == "cnn":
        model_init_fn = models.initialize_regression_cnn
    else: # cl_args.model == "mlp"
        model_init_fn = models.initialize_regression_mlp
    input_dim, output_dim = _compute_io_dims(cl_args.dataset)
    model_init_fn = partial(model_init_fn, input_dim=input_dim, 
                            output_dim=output_dim, 
                            emission_cov=cl_args.obs_scale**2)
    
    # Set up agents
    agents = _process_agent_args(cl_args.agents, cl_args.tune_sgd_momentum,
                                 cl_args.ranks, input_dim, output_dim,
                                 cl_args.problem, cl_args.obs_scale, 
                                 cl_args.nll_method)
    
    # Set up hyperparameter tuning
    hparam_path = Path(config_path, problem_str, cl_args.dataset,
                       cl_args.model, nll_method)
    if cl_args.hyperparameters != "eval_only":
        agent_hparams = \
            tune_and_store_hyperparameters(hparam_path, model_init_fn, 
                                           dataset_load_fn, agents,
                                           eval_metric["val"], cl_args.verbose, 
                                           cl_args.n_explore, cl_args.n_exploit,
                                           cl_args.temp, cl_args.nll_method)
    else:
        agent_hparams = {}
        for agent_name in agents:
            # Check if hyperparameters are specified in config file
            agent_filepath = agent_name
            if cl_args.temp != 1.0:
                agent_filepath = f"{agent_name}-temp-{cl_args.temp}"
            agent_hparam_path = Path(hparam_path, agent_filepath+".json")
            try:
                # Load json file
                with open(agent_hparam_path, "r") as f:
                    agent_hparams[agent_name] = json.load(f)
            except FileNotFoundError:
                raise FileNotFoundError(f"Hyperparameter {agent_hparam_path} "
                                        "not found.")
    
    if cl_args.hyperparameters != "tune_only":
        # Evaluate agents
        kwargs["scale"] = cl_args.obs_scale
        for agent_name, hparams in agent_hparams.items():
            print(f"Evaluating {agent_name}...")
            agent_kwargs = agents[agent_name]
            if "pbounds" in agent_kwargs:
                agent_kwargs.pop("pbounds")
            optimizer_dict = hparam_tune.build_estimator(model_init_fn, hparams,
                                                        agent_name, 
                                                        classification=False,
                                                        **agent_kwargs)
            _ = evaluate_and_store_result(output_path, model_init_fn,
                                        dataset_load_fn, optimizer_dict,
                                        eval_metric["test"], agent_name,
                                        cl_args.problem, cl_args.n_iter,
                                        temperature=cl_args.temp, **kwargs)
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    
    # Problem type (stationary, permuted, rotated, or split)
    parser.add_argument("--problem", type=str, default="iid",
                        choices=["iid", "amplified", "random-walk", "permuted"])
    
    # Type of dataset
    parser.add_argument("--dataset", type=str, default="fashion_mnist", 
                        choices=["mnist", "fashion_mnist", 
                                 "cifar10", "cifar100"])
    
    # Target digit
    parser.add_argument("--target_digit", type=int, default=2,
                        choices=[-1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    
    # Number of training samples
    parser.add_argument("--ntrain", type=_check_positive_int, default=2_000)
    
    # Type of model (mlp or cnn)
    parser.add_argument("--model", type=str, default="mlp",
                        choices=["mlp", "cnn"])
    
    # Observation noise
    parser.add_argument("--obs_scale", type=_check_nonneg_float, default=15.0)
    
    # Negative log likelihood evaluation method
    parser.add_argument("--nll_method", type=str, default="nll", 
                        choices=["nll", "nlpd-mc", "nlpd-linearized"])
    
    # Multiplicative factor for aleatoric uncertainty
    parser.add_argument("--aleatoric", type=_check_nonneg_float, default=1.0)
    
    # Temperature for NLPD-MC sampling
    parser.add_argument("--temp", type=_check_nonneg_float, default=1.0)
    
    # Tune the hyperparameters of the agents
    parser.add_argument("--hyperparameters", type=str, default="tune_and_eval",
                        choices=["tune_and_eval", "tune_only", "eval_only"])
    
    # Set the number of exploration steps
    parser.add_argument("--n_explore", type=int, default=10)
    
    # Set the number of exploitation steps
    parser.add_argument("--n_exploit", type=int, default=15)
    
    # Set the verbosity of the Bayesopt procedure
    parser.add_argument("--verbose", type=int, default=2,
                        choices=[0, 1, 2])
    
    # List of ranks to use for the agents
    parser.add_argument("--ranks", type=_check_positive_int, nargs="+",
                        default=[1, 10,])
    
    # List of agents to use
    parser.add_argument("--agents", type=str, nargs="+", default=AGENT_TYPES,
                        choices=AGENT_TYPES)
    
    # Tune momentum for SGD
    parser.add_argument("--tune_sgd_momentum", action="store_true")
    
    # Number of random initializations for evaluation
    parser.add_argument("--n_iter", type=int, default=100)
    
    args = parser.parse_args()
    main(args)
    