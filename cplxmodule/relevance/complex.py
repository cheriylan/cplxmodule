import torch
import torch.nn

import torch.nn.functional as F

from math import sqrt
from numpy import euler_gamma

from .base import BaseLinearARD

from .utils import kldiv_approx, torch_expi
from .utils import torch_sparse_cplx_linear, torch_sparse_tensor

from ..layers import CplxLinear
from ..cplx import Cplx, cplx_linear


def cplx_nkldiv_apprx(log_alpha, reduction="mean"):
    r"""
    Sofplus-sigmoid approximation of the negative complex KL divergence.
    $$
        - KL(\mathcal{CN}(w\mid \theta, \alpha \theta \bar{\theta}, 0) \|
                \tfrac1{\lvert w \rvert^2})
            = \log \alpha
              - 2 \mathbb{E}_{\xi \sim \mathcal{CN}(1, \alpha, 0)}
                \log{\lvert \xi \rvert} + C
        \,. $$
    For coef estimation and derivation c.f. the supplementary notebook.
    """
    coef = 0.57811, 1.46018, 1.36562, 1.  # 0.57811265, 1.4601848, 1.36561527
    return kldiv_approx(log_alpha, coef, reduction)


def cplx_nkldiv_exact(log_alpha, reduction="mean"):
    r"""
    Exact negative complex KL divergence
    $$
        - KL(\mathcal{CN}(w\mid \theta, \alpha \theta \bar{\theta}, 0) \|
                \tfrac1{\lvert w \rvert^2})
            = \log \alpha
              - 2 \mathbb{E}_{\xi \sim \mathcal{CN}(1, \alpha, 0)}
                \log{\lvert \xi \rvert} + C
            = \log \alpha + Ei( - \tfrac1{\alpha}) + C
        \,, $$
    where $Ei(x) = \int_{-\infty}^x e^t t^{-1} dt$ is the exponential integral.
    """
    if reduction is not None and reduction not in ("mean", "sum"):
        raise ValueError("""`reduction` must be either `None`, "sum" """
                         """or "mean".""")

    # Ei behaves well on the -ve values, and near 0-.
    kl_div = log_alpha + torch_expi(- torch.exp(- log_alpha)) - euler_gamma

    if reduction == "mean":
        return kl_div.mean()

    elif reduction == "sum":
        return kl_div.sum()

    return kl_div


class CplxLinearARD(CplxLinear, BaseLinearARD):
    def __init__(self, in_features, out_features, bias=True, exact=True):
        super().__init__(in_features, out_features, bias=bias)

        self.exact = exact
        self.log_sigma2 = torch.nn.Parameter(
            torch.Tensor(out_features, in_features))
        self.reset_variational_parameters()

    def reset_variational_parameters(self):
        self.log_sigma2.data.uniform_(-10, -10)  # wtf?

    @property
    def log_alpha(self):
        r"""Get $\log \alpha$ from $(\theta, \sigma^2)$ parameterization."""
        # $\alpha = \tfrac{\sigma^2}{\theta \bar{\theta}}$
        abs_weight = abs(Cplx(**self.weight))
        return self.log_sigma2 - 2 * torch.log(abs_weight + 1e-12)

    @property
    def penalty(self):
        r"""Compute the variational penalty term."""
        # neg KL divergence must be maximized, hence the -ve sign.
        if self.exact:
            return -cplx_nkldiv_exact(self.log_alpha, reduction="mean")

        return -cplx_nkldiv_apprx(self.log_alpha, reduction="mean")

    def forward(self, input):
        if not self.training and self.is_sparse:
            return self.forward_sparse(input)

        # $\mu = \theta x$ in $\mathbb{C}$
        mu = super().forward(input)
        # mu = cplx_linear(input, Cplx(**self.weight), self.bias)
        if not self.training:
            return mu

        # \gamma = \sigma^2 (x \odot \bar{x})
        s2 = F.linear(input.real * input.real + input.imag * input.imag,
                      torch.exp(self.log_sigma2), None)

        # generate complex gaussian noise with proper scale
        noise = Cplx(*map(torch.rand_like, (s2, s2))) / sqrt(2)
        return mu + noise * torch.sqrt(s2 + 1e-12)

    def forward_sparse(self, input):
        weight = Cplx(self.sparse_re_weight_, self.sparse_im_weight_)
        if self.sparsity_mode_ == "dense":
            return cplx_linear(input, weight, self.bias)

        return torch_sparse_cplx_linear(input, weight, self.bias)

    def sparsify(self, threshold=1.0, mode="dense"):
        if mode is not None and mode not in ("dense", "sparse"):
            raise ValueError(f"""`mode` must be either 'dense', 'sparse' or """
                             f"""`None` (got '{mode}').""")

        if mode is not None and self.training:
            raise RuntimeError("Cannot sparsify model while training.")

        self.sparsity_mode_ = mode
        if mode is not None:
            mask = ~self.get_sparsity_mask(threshold)

            if mode == "sparse":
                indices = mask.nonzero().t()
                re_weight = torch_sparse_tensor(
                    indices, self.weight.real[mask], self.weight.real.shape)
                im_weight = torch_sparse_tensor(
                    indices, self.weight.imag[mask], self.weight.imag.shape)

            elif mode == "dense":
                zero = torch.tensor(0.).to(self.weight.real)
                re_weight = torch.where(mask, self.weight.real, zero)
                im_weight = torch.where(mask, self.weight.imag, zero)

            self.register_buffer("sparse_re_weight_", re_weight)
            self.register_buffer("sparse_im_weight_", im_weight)

        else:
            if hasattr(self, "sparse_re_weight_"):
                del self.sparse_re_weight_
            if hasattr(self, "sparse_im_weight_"):
                del self.sparse_im_weight_

        # end if

        return self

    def num_zeros(self, threshold=1.0):
        return 2 * self.get_sparsity_mask(threshold).sum().item()
