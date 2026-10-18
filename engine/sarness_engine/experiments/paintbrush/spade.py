"""SPADE generator (GauGAN) for the community "Labels2Landscapes_512" checkpoint.

Architecture as in NVlabs/SPADE (``netG=spade``, ``ngf=64``,
``num_upsampling_layers=normal``, ``norm_G=spectralspadesyncbatch3x3``,
``use_vae=False``, ``label_nc=182``, ``no_instance=True``), written out so the
checkpoint loads with no third-party SPADE code. Synchronised batch norm is a
plain ``BatchNorm2d`` at inference time (same parameters and running stats), and
spectral normalisation is folded into the weights after loading.

Worker process only: importing this module imports torch.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import spectral_norm

LABEL_NC = 182
NGF = 64


class SPADE(nn.Module):
    def __init__(self, norm_nc: int, label_nc: int = LABEL_NC, hidden: int = 128):
        super().__init__()
        self.param_free_norm = nn.BatchNorm2d(norm_nc, affine=False)
        self.mlp_shared = nn.Sequential(nn.Conv2d(label_nc, hidden, 3, padding=1), nn.ReLU())
        self.mlp_gamma = nn.Conv2d(hidden, norm_nc, 3, padding=1)
        self.mlp_beta = nn.Conv2d(hidden, norm_nc, 3, padding=1)

    def forward(self, x: torch.Tensor, seg: torch.Tensor) -> torch.Tensor:
        normalized = self.param_free_norm(x)
        seg = F.interpolate(seg, size=x.shape[2:], mode="nearest")
        actv = self.mlp_shared(seg)
        return normalized * (1 + self.mlp_gamma(actv)) + self.mlp_beta(actv)


class SPADEResnetBlock(nn.Module):
    def __init__(self, fin: int, fout: int):
        super().__init__()
        self.learned_shortcut = fin != fout
        fmiddle = min(fin, fout)
        self.conv_0 = spectral_norm(nn.Conv2d(fin, fmiddle, 3, padding=1))
        self.conv_1 = spectral_norm(nn.Conv2d(fmiddle, fout, 3, padding=1))
        if self.learned_shortcut:
            self.conv_s = spectral_norm(nn.Conv2d(fin, fout, 1, bias=False))
            self.norm_s = SPADE(fin)
        self.norm_0 = SPADE(fin)
        self.norm_1 = SPADE(fmiddle)

    def forward(self, x: torch.Tensor, seg: torch.Tensor) -> torch.Tensor:
        x_s = self.conv_s(self.norm_s(x, seg)) if self.learned_shortcut else x
        dx = self.conv_0(F.leaky_relu(self.norm_0(x, seg), 0.2))
        dx = self.conv_1(F.leaky_relu(self.norm_1(dx, seg), 0.2))
        return x_s + dx


class SPADEGenerator(nn.Module):
    def __init__(self, nf: int = NGF):
        super().__init__()
        self.fc = nn.Conv2d(LABEL_NC, 16 * nf, 3, padding=1)
        self.head_0 = SPADEResnetBlock(16 * nf, 16 * nf)
        self.G_middle_0 = SPADEResnetBlock(16 * nf, 16 * nf)
        self.G_middle_1 = SPADEResnetBlock(16 * nf, 16 * nf)
        self.up_0 = SPADEResnetBlock(16 * nf, 8 * nf)
        self.up_1 = SPADEResnetBlock(8 * nf, 4 * nf)
        self.up_2 = SPADEResnetBlock(4 * nf, 2 * nf)
        self.up_3 = SPADEResnetBlock(2 * nf, 1 * nf)
        self.conv_img = nn.Conv2d(nf, 3, 3, padding=1)

    def forward(self, seg: torch.Tensor) -> torch.Tensor:
        # The latent grid is 1/32 of the label map, so any multiple of 32 works (the
        # reference code fixes it from crop_size; deriving it keeps non-square canvases sharp).
        h, w = seg.shape[2] // 32, seg.shape[3] // 32
        x = self.fc(F.interpolate(seg, size=(h, w)))
        x = self.head_0(x, seg)
        x = F.interpolate(x, scale_factor=2)
        x = self.G_middle_0(x, seg)
        x = self.G_middle_1(x, seg)
        for block in (self.up_0, self.up_1, self.up_2, self.up_3):
            x = F.interpolate(x, scale_factor=2)
            x = block(x, seg)
        return torch.tanh(self.conv_img(F.leaky_relu(x, 0.2)))


def load_generator(path: str, device: str = "cuda", dtype: torch.dtype = torch.float16) -> SPADEGenerator:
    state = torch.load(path, map_location="cpu", weights_only=True)
    g = SPADEGenerator()
    missing, unexpected = g.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"checkpoint does not match the SPADE landscape generator "
                           f"(missing {len(missing)}, unexpected {len(unexpected)} keys)")
    for m in g.modules():
        if hasattr(m, "weight_orig"):
            torch.nn.utils.remove_spectral_norm(m)
    return g.eval().to(device=device, dtype=dtype).to(memory_format=torch.channels_last)


def one_hot(coco: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
    """``(H, W)`` int64 COCO-Stuff ids → ``(1, 182, H, W)`` one-hot."""
    h, w = coco.shape
    seg = torch.zeros(1, LABEL_NC, h, w, device=coco.device, dtype=dtype)
    return seg.scatter_(1, coco.view(1, 1, h, w), 1.0).contiguous(memory_format=torch.channels_last)
