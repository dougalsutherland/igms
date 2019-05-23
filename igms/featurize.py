from copy import copy
import os
import types

import torch
from torch import nn
from torchvision.models.resnet import ResNet, resnet50

################################################################################
# Normalization layer, since we need to do this to generator samples instead of
# on loading the data.

# These are for data in [0, 1]
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STDDEV = (0.229, 0.224, 0.225)


class NormalizeLayer(torch.nn.Module):
    def __init__(self, means=_IMAGENET_MEAN, sds=_IMAGENET_STDDEV, **kwargs):
        super().__init__()
        self.register_buffer("means", torch.as_tensor(means, **kwargs))
        self.register_buffer("sds", torch.as_tensor(sds, **kwargs))

    def forward(self, input):
        assert len(input.shape) == 4
        cent = input - self.means[None, :, None, None]
        return cent / self.sds[None, :, None, None]


################################################################################
# Registry of forward() monkey patches to extract features instead of getting
# classification outputs.

_registry = {}


def extractor(cls, kind):
    def decorator(f):
        _registry[cls, kind] = f
        return f

    return decorator


@extractor(ResNet, "end")
def last_hidden(self, x):
    x = self.conv1(x)
    x = self.bn1(x)
    x = self.relu(x)
    x = self.maxpool(x)

    x = self.layer1(x)
    x = self.layer2(x)
    x = self.layer3(x)
    x = self.layer4(x)

    x = self.avgpool(x)
    x = x.view(x.size(0), -1)
    # x = self.fc(x)
    return x


@extractor(ResNet, "through")
def all_intermediate(self, x):
    bits = []

    def add():
        bits.append(x.view(x.size(0), -1))

    x = self.conv1(x)
    x = self.bn1(x)
    x = self.relu(x)
    x = self.maxpool(x)
    add()

    x = self.layer1(x)
    add()
    x = self.layer2(x)
    add()
    x = self.layer3(x)
    add()
    x = self.layer4(x)
    add()

    x = self.avgpool(x)
    add()
    # x = x.view(x.size(0), -1)
    # x = self.fc(x)

    return torch.cat(bits, 1)


def patch(model, kind):
    try:
        f = _registry[type(model), kind]
    except KeyError:
        raise TypeError(f"Don't know how to patch {kind} for type {type(model)}")

    model.forward = types.MethodType(f, model)


################################################################################
# Main method to load stuff.


def make_featurizer(model, kind="through", input_scale=(-1, 1)):
    """
    Makes a featurizer that *shares data* with model, but whose forward()
    computes features instead of the final output, according to what's
    registered with @extractor above.

    Assumes that model wants input like torchvision models, i.e. z-scored
    according to IMAGENET_MEAN, _IMAGENET_STDEV above, but takes input inside
    input_scale (default [-1, 1]). If input_scale is None, doesn't do any
    scaling.

    Calls model.eval() and sets requires_grad to False for parameters inside
    `model` as well, since there doesn't seem to be a way to do this only on
    the copy.
    """
    featurizer = copy(model)
    patch(featurizer, kind)
    featurizer.eval()  # v important for batch norm!
    for p in featurizer.parameters():
        p.requires_grad = False

    if input_scale is None:
        return featurizer

    lo, hi = input_scale
    scale = hi - lo
    means = [m * scale + lo for m in _IMAGENET_MEAN]
    stds = [s * scale for s in _IMAGENET_STDDEV]
    norm_layer = NormalizeLayer(means, stds)
    return nn.Sequential(norm_layer, featurizer)


def load_smoothing_imagenet_model(checkpoint_path, **kwargs):
    def rewrite(k):  # load parameters directly into the model
        assert k.startswith("1.module.")
        return k[9:]

    sd = {rewrite(k): v for k, v in torch.load(checkpoint_path)["state_dict"].items()}

    model = resnet50(pretrained=False).to(**kwargs)
    model.load_state_dict(sd)
    return model


def load_featurizer(spec, input_scale=(-1, 1), **to_kwargs):
    parts = spec.split(":")

    kind = parts.pop(0) if parts else "through"

    model_type = parts.pop(0) if parts else "smoothing"
    if model_type == "smoothing":
        def_pth = "~/smoothing/models/imagenet/resnet50/noise_0.25/checkpoint.pth.tar"
        pth = parts.pop(0) if parts else def_pth
        model = load_smoothing_imagenet_model(os.path.expanduser(pth), **to_kwargs)
    else:
        raise ValueError(f"unknown model type {model_type}")

    return make_featurizer(model, kind=kind, input_scale=input_scale)