"""
Implementation of "Convolutional Sequence to Sequence Learning"
"""
import torch
import torch.nn as nn
import torch.nn.init as init

import mammoth.modules

SCALE_WEIGHT = 0.5**0.5


def shape_transform(x):
    """Tranform the size of the tensors to fit for conv input."""
    return torch.unsqueeze(torch.transpose(x, 1, 2), 3)


class GatedConv(nn.Module):
    """Gated convolution for CNN class"""

    def __init__(self, input_size, width=3, dropout=0.2, nopad=False):
        super(GatedConv, self).__init__()
        self.conv = mammoth.modules.WeightNormConv2d(
            input_size, 2 * input_size, kernel_size=(width, 1), stride=(1, 1), padding=(width // 2 * (1 - nopad), 0)
        )
        init.xavier_uniform_(self.conv.weight, gain=(4 * (1 - dropout)) ** 0.5)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x_var):
        x_var = self.dropout(x_var)
        x_var = self.conv(x_var)
        out, gate = x_var.split(int(x_var.size(1) / 2), 1)
        out = out * torch.sigmoid(gate)
        return out
