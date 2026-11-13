"""Multiple imputation for continuous outcomes with missing covariates.

Pipeline: validate plan + inputs -> diagnose -> iterative conditional
imputation (FCS) with Bayesian linear draws and predictive mean matching
-> fit one frozen linear model per completed dataset -> Rubin pooling.

Scope: continuous variables only, response fully observed, no binary /
logistic sub-models, no missing-mechanism identification.
"""

__version__ = "0.1.0"
