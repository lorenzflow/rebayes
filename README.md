# ReBayes = Recursive Bayesian inference for latent states

We provide code for online (recursive) Bayesian inference in state space models;
in contrast to the dynamax code, we do not assume the entire observation sequence is available in advance,
so the ReBayes API can be used in an interactive loop (e.g., for Bayesian optimization).
We assume the dynamics model is linear Gaussian (or constant),
but the observation model can be non-linear and non-Gaussian.

This is work in progress; a stable version will be releaed late Spring 2023.


