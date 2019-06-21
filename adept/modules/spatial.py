# Copyright (C) 2018 Heron Systems, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import torch
from torch import nn as nn
from torch.nn import functional as F


class Residual2DPreact(nn.Module):
    def __init__(self, nb_in_chan, nb_out_chan, stride=1):
        super(Residual2DPreact, self).__init__()

        self.nb_in_chan = nb_in_chan
        self.nb_out_chan = nb_out_chan
        self.stride = stride

        self.bn1 = nn.BatchNorm2d(nb_in_chan)
        self.conv1 = nn.Conv2d(
            nb_in_chan, nb_out_chan, 3, stride=stride, padding=1, bias=False
        )
        self.bn2 = nn.BatchNorm2d(nb_out_chan)
        self.conv2 = nn.Conv2d(
            nb_out_chan, nb_out_chan, 3, stride=1, padding=1, bias=False
        )

        relu_gain = nn.init.calculate_gain('relu')
        self.conv1.weight.data.mul_(relu_gain)
        self.conv2.weight.data.mul_(relu_gain)

        self.do_projection = self.nb_in_chan != self.nb_out_chan or self.stride > 1
        if self.do_projection:
            self.projection = nn.Conv2d(
                nb_in_chan, nb_out_chan, 3, stride=stride, padding=1
            )
            self.projection.weight.data.mul_(relu_gain)

    def forward(self, x):
        first = F.relu(self.bn1(x))
        if self.do_projection:
            projection = self.projection(first)
        else:
            projection = x
        x = self.conv1(first)
        x = self.conv2(F.relu(self.bn2(x)))
        return x + projection


class ConvLSTMCellLayerNorm(nn.Module):
    """
    A lstm cell that layer norms the cell state
    https://github.com/seba-1511/lstms.pth/blob/master/lstms/lstm.py for reference.
    Original License Apache 2.0

    Modified to follow tensorflow implementation here:
    https://github.com/tensorflow/tensorflow/blob/r1.13/tensorflow/contrib/rnn/python/ops/rnn_cell.py#L2453
    """

    def __init__(self, input_size, hidden_size, kernel_size, stride=1, padding=0, forget_bias=1.0):
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.forget_bias = forget_bias
        # hidden to hidden must be the same size
        self._hidden_padding = int((kernel_size - 1) / 2)
        self.ih = nn.Conv2d(input_size[0], 4 * hidden_size, kernel_size, stride=stride, padding=padding, bias=False)
        self.hh = nn.Conv2d(hidden_size, 4 * hidden_size, kernel_size, padding=self._hidden_padding, bias=False)

        hh_input_size = calc_conv_output_dim(input_size[1], kernel_size, stride, padding, 1)

        self.ln_g_t = nn.LayerNorm([hidden_size, hh_input_size, hh_input_size])
        self.ln_i_t = nn.LayerNorm([hidden_size, hh_input_size, hh_input_size])
        self.ln_f_t = nn.LayerNorm([hidden_size, hh_input_size, hh_input_size])
        self.ln_o_t = nn.LayerNorm([hidden_size, hh_input_size, hh_input_size])
        self.ln_cell = nn.LayerNorm([hidden_size, hh_input_size, hh_input_size])

    def forward(self, x, hidden):
        """
        LSTM Cell that layer normalizes the cell state.
        :param x: Tensor{B, C}
        :param hidden: A Tuple[Tensor{B, C}, Tensor{B, C}] of (previous output, cell state)
        :return:
        """
        h, c = hidden

        # Linear mappings
        i2h = self.ih(x)
        h2h = self.hh(h)
        preact = i2h + h2h

        # activations, chunk over channels
        it, ft, ot, gt = torch.chunk(preact, 4, dim=1)
        i_t = self.ln_i_t(it).sigmoid_()
        f_t = self.ln_f_t(ft)
        # forget bias
        if self.forget_bias != 0:
            f_t += self.forget_bias
            f_t.sigmoid_()
        o_t = self.ln_o_t(ot).sigmoid_()
        g_t = self.ln_g_t(gt).tanh_()

        # cell computations cannot be inplace 
        c_t = torch.mul(c, f_t) + torch.mul(i_t, g_t)
        c_t = self.ln_cell(c_t)
        h_t = torch.mul(o_t, c_t.tanh())

        return h_t, c_t


def calc_conv_output_dim(dim_size, kernel_size, stride, padding, dilation):
    numerator = dim_size + 2 * padding - dilation * (kernel_size - 1) - 1
    return numerator // stride + 1
