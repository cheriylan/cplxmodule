# Real- and Complex- valued Variational Dropout

This is the core module for the real- and complex- valued Bayesian sparsification.

## Usage

The basic pipeline for applying Bayesian sparsification methods is to train a non-Bayesian model and then promote the select layers to their Bayesian variants. A non pytorch-friendly way is to perform surgery on the existing model, replacing layers (in `._modules` ordered dict) and copying weight. A more transparent and orthodox method is to pass a type substitution dict to `__init__` and propagate it to submodules.

Below is a real-valued classifier (Complex-valued is similar, but requires input of type `cplx.Cplx`):

```python
from torch.nn import Module, Sequential, Flatten, ReLU
from torch.nn import Linear

from cplxmodule.nn.relevance.extensions import LinearARD
from cplxmodule.nn.masked import LinearMasked


class MNISTFullyConnected(Module):
    def __init__(self, n_hidden=400, **types):
        super().__init__()

        linear = types.get("linear", Linear)

        # features just flatten the channel and spatial dims
        self.features = Sequential(
            Flatten(-3, -1)
        )

        # use the provided Linear layer type
        self.classifier = Sequential(
            linear(28 * 28, n_hidden, bias=True),
            ReLU(),
            linear(n_hidden, 10, bias=True),
        )

    def forward(self, input):
        return self.classifier(self.features(input))


models = {
    "dense": MNISTFullyConnected(400),
    "bayes": MNISTFullyConnected(400, linear=LinearARD),
    "masked": MNISTFullyConnected(400, linear=LinearMasked)
}
```

Important: complex-valued alternatives can be readily plugged in instead of real-valued layers, except one would have to prepend a real-to-complex transformation layer to `features`, and append a complex-to-real transformation to the `classifier`.

### Collecting KL divergence terms for the loss

The variational dropout and relevance determination techniques require a penalty term to be introduced to the loss objective. The term is given by the Kullback-Leibler divergences of the variational approximation from the assumed prior distribution.

Each layer, which inherits from `BaseARD`, is responsible for computing the KL divergence terms related to the variational approximations of and only its own parameters, e.g. children submodules in turn compute their own divergences. Therefore the layer must implement a the `.penalty` read-only property, which is responsible for computing the divergence.

The following functions return generators, that yield the penalties of all eligible submodules. If a layer is not a subclass of `BaseARD` then it is ignored.

* `named_penalties(module, reduction="sum", prefix='')` much like the `.named_modules` method of any pytorch Module, this generator yields submodule's name and penalty value pairs. The penalty values are taken from `.penalty` and reduced, depending on the `reduction` setting.

* `penalties(module, reduction="sum")` the same as `named_penalties()`, but yields the penalty values only. Handy if one needs a quick way to accumulate penalties into the loss expression (sum of an empty iterator is always zero):

```python
from cplxmodule.nn.relevance import penalties

model = models["dense"]  # models["bayes"] or even models["masked"]

# `coef` has the most profound effect on sparsity, `threshold` -- not so much
coef = 1e-2 / effective_dataset_size

# ... somewhere inside the train loop.
loss = criterion(model(X), y) + coef * sum(penalties(model, reduction="sum"))
```

### Transferring learnt weights from/to non-variational modules

Since variational approximations use additive noise reparameterization, each variational dropout module in `nn.relevance` has a `log_sigma2` learnable parameter, used to compute `\log \alpha` on-the-fly when needed by `.penalty` or `.log_alpha` read-only properties. 

Deploying trained weights from a non-Bayesian model to a Bayesian requires
```python
state_dict = models["dense"].state_dict()

models["bayes"].load_state_dict(state_dict,  strict=False)
```
Since non-Bayesian models do not model their parameters through factorized Gaussian variational approximation, the above operation transfers copies weight into the means of these distributions. It is necessary to set `strict=False` in `.load_state_dict()`, since non-Bayesian models lack `.log_sigma2` parameters, which are initialized to `-10` upon Bayesian layer instantiation. This effectively renders the great bulk of copied weights relevant.

Transferring in the opposiute direction requires `strict=False` as well, since `.log_sigma2` have to be ignored by the receiving model.

### Computing relevance masks

The variational dropout and relevance determination methods use special Fully Factorised Gaussian approximation with mean `\mu` and variance `\alpha \lvert \mu \rvert^2`. The `\alpha` is essentially the ratio of mean to standard deviation and is learnt either directly, or through additive reparameterization. It effectively scores the irrelevance of the parameter it is associated with: `\alpha` is close to zero, then the parameter is more relevant, rather than the parameter with `\alpha` above `1`.

In order to decide if a parameter is relevant it is necessary to compare its irrelevance score against a threshold. The following functions can be used for returning the masks of kept/dropped out (sparsified) parametersL

* `named_relevance(module, threshold=..., hard=True)` much like the `.named_penalties`, this generator yields submodule's name and the computed relevance mask, which is `nonzero` at those parameter elements, which have `\log\alpha` below the given `threshold`. `hard` forces the returned mask to be binary.

* `compute_ard_masks(module, threshold=..., hard=True)` also returns the sparsity mask, but unlike `named_relevance` returns a dictionary of masks, keyed by parameter manes compatible with the masking interface of the layers in `nn.masked`.

### Interfacing with masked layers

Layer sparsification with Bayesian dropout layers is performed in two steps:
1. relevance masks are computed based on the used-supplied threshold for `\log \alpha`
2. masks are merged the layers parameters and deployed into a maskable layer

Below is a handy recipe to facilitate mask transfer:

```python
from cplxmodule.nn.relevance import compute_ard_masks
from cplxmodule.nn.masked import binarize_masks


def state_dict_with_masks(model, **kwargs):
    """Harvest and binarize masks, then cleanup the zeroed parameters."""
    with torch.no_grad():
        masks = compute_ard_masks(model, **kwargs)
        state_dict, masks = binarize_masks(model.state_dict(), masks)

    state_dict.update(masks)
    return state_dict, masks

# threshold of -0.5 lose in performance a little, but gives much stronger sparsity
state_dict, masks = state_dict_with_masks(models["bayes"], threshold=-0.5, hard=True)

# state dict for loading, masks for analysis
models["masked"].load_state_dict(state_dict,  strict=False)
```
Masked layers have only the `.weight` parameter (optionally `.bias`), hence it is necessary to set `strict=False` in `.load_state_dict()`.

## Implementation

### Modules

The modules in `nn.relevance` implement both `real`- and `complex` valued variational dropout methods. Due to poor naming, used in earlier version of the library the naming of variational dropout method and automatic relevance determination were mixed up. As of *2020-02-28* the naming in `nn.relevance.real` and `nn.relevance.complex`  must be disregarded and correctly named real and complex valued layers must be imported from `nn.relevance.extensions`.

* Variational dropout (log-uniform prior)
    - (real) LinearVD, Conv1dVD, Conv2dVD, BilinearVD
    - (complex) CplxLinearVD, CplxConv1dVD, CplxConv2dVD, CplxBilinearVD

* Automatic Relevance Determination (factorized gaussian prior with learnt precision)
    - (real) LinearARD, Conv1dARD, Conv2dARD, BilinearARD
    - (complex) CplxLinearARD, CplxConv1dARD, CplxConv2dARD, CplxBilinearARD

* Variational dropout with bogus forward values, but exact gradients
    - (complex only) CplxLinearVDBogus, CplxConv1dVDBogus, CplxConv2dVDBogus, CplxBilinearVDBogus

### Subclassing Modules

Since `BaseARD` does not have `__init__`, it can be placed anywhere in the list of base classes in the definition when subclassing or multiply inheriting from `BaseARD`.

## Compatibility

As of 2020-02-28 the modules and their API was designed so as to comply with the public interface exposed in pytorch 1.4.