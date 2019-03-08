# Author: Jose A. R. Fonollosa, Universitat Politecnica de Catalunya.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from fairseq import options
from fairseq import utils

from fairseq.modules import (
    AdaptiveInput, AdaptiveSoftmax, CharacterTokenEmbedder, LearnedPositionalEmbedding, MultiheadAttention,
    SinusoidalPositionalEmbedding
)

from fairseq.models import (
    FairseqIncrementalDecoder, FairseqEncoder, FairseqModel, register_model, register_model_architecture,
)

from fairseq.models.transformer import TransformerDecoderLayer

@register_model('joint_source_target')
class JointSourceTargetModel(FairseqModel):
    """
    Local Joint Source-Target model from `"Joint Source-Target Self Attention with Locality Constraints" (Fonollosa, et al, 2019)
    <https://>`_.

    Args:
        encoder (JointSourceTargetEncoder): the encoder
        decoder (JointSourceTargetDecoder): the decoder

    The joint source-target model provides the following named architectures and
    command-line arguments:

    .. argparse::
        :ref: fairseq.models.joint_source_target_parser
        :prog:
    """

    def __init__(self, encoder, decoder):
        super().__init__(encoder, decoder)

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        parser.add_argument('--dropout', type=float, metavar='D',
                            help='dropout probability')
        parser.add_argument('--attention-dropout', type=float, metavar='D',
                            help='dropout probability for attention weights')
        parser.add_argument('--relu-dropout', type=float, metavar='D',
                            help='dropout probability after ReLU in FFN')
        parser.add_argument('--input-dropout', type=float, metavar='D',
                            help='dropout probability of the inputs')
        parser.add_argument('--encoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained encoder embedding')
        parser.add_argument('--encoder-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension')
        parser.add_argument('--encoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the encoder')
        parser.add_argument('--decoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained decoder embedding')
        parser.add_argument('--decoder-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension')
        parser.add_argument('--decoder-ffn-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension for FFN')
        parser.add_argument('--decoder-layers', type=int, metavar='N',
                            help='num decoder layers')
        parser.add_argument('--decoder-attention-heads', type=int, metavar='N',
                            help='num decoder attention heads')
        parser.add_argument('--decoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the decoder')
        parser.add_argument('--decoder-normalize-before', action='store_true',
                            help='apply layernorm before each decoder block')
        parser.add_argument('--share-decoder-input-output-embed', action='store_true',
                            help='share decoder input and output embeddings')
        parser.add_argument('--share-all-embeddings', action='store_true',
                            help='share encoder, decoder and output embeddings'
                                 ' (requires shared dictionary and embed dim)')
        parser.add_argument('--adaptive-softmax-cutoff', metavar='EXPR',
                            help='comma separated list of adaptive softmax cutoff points. '
                                 'Must be used with adaptive_loss criterion'),
        parser.add_argument('--adaptive-softmax-dropout', type=float, metavar='D',
                            help='sets adaptive softmax dropout for the tail projections')
        parser.add_argument('--language-embeddings', action='store_true',
                            help='use language embeddings')

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present in older models
        base_architecture(args)

        if not hasattr(args, 'max_source_positions'):
            args.max_source_positions = 1024
        if not hasattr(args, 'max_target_positions'):
            args.max_target_positions = 1024

        src_dict, tgt_dict = task.source_dictionary, task.target_dictionary

        def build_embedding(dictionary, embed_dim, path=None):
            num_embeddings = len(dictionary)
            padding_idx = dictionary.pad()
            emb = Embedding(num_embeddings, embed_dim, padding_idx)
            # if provided, load from preloaded dictionaries
            if path:
                embed_dict = utils.parse_embedding(path)
                utils.load_embedding(embed_dict, dictionary, emb)
            return emb

        if args.share_all_embeddings:
            if src_dict != tgt_dict:
                raise ValueError('--share-all-embeddings requires a joined dictionary')
            if args.encoder_embed_dim != args.decoder_embed_dim:
                raise ValueError(
                    '--share-all-embeddings requires --encoder-embed-dim to match --decoder-embed-dim')
            if args.decoder_embed_path and (
                    args.decoder_embed_path != args.encoder_embed_path):
                raise ValueError('--share-all-embeddings not compatible with --decoder-embed-path')
            encoder_embed_tokens = build_embedding(
                src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = encoder_embed_tokens
            args.share_decoder_input_output_embed = True
        else:
            raise RuntimeError('The joint_source_target model requires --share-all-embeddings')

        encoder = JointSourceTargetEncoder(args, src_dict, encoder_embed_tokens, left_pad=args.left_pad_source)
        decoder = JointSourceTargetDecoder(args, tgt_dict, decoder_embed_tokens, left_pad=args.left_pad_target)
        return JointSourceTargetModel(encoder, decoder)


class JointSourceTargetEncoder(FairseqEncoder):
    """
    JointSourceTarget encoder is used only to compute the source embeddings.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): encoding dictionary
        embed_tokens (torch.nn.Embedding): input embedding
        left_pad (bool): whether the input is left-padded
    """

    def __init__(self, args, dictionary, embed_tokens, left_pad):
        super().__init__(dictionary)
        self.dropout = args.dropout

        embed_dim = embed_tokens.embedding_dim
        self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = args.max_source_positions

        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(embed_dim)
        self.embed_positions = PositionalEmbedding(
            args.max_source_positions, embed_dim, self.padding_idx,
            left_pad=left_pad,
            learned=args.encoder_learned_pos,
        ) if not args.no_token_positional_embeddings else None
        self.embed_language = LanguageEmbedding(embed_dim) if args.language_embeddings else None

        self.register_buffer('version', torch.Tensor([2]))

    def forward(self, src_tokens, src_lengths):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (torch.LongTensor): lengths of each source sentence of
                shape `(batch)`

        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
        """
        # embed tokens and positions
        x = self.embed_scale * self.embed_tokens(src_tokens)
        if self.embed_positions is not None:
            x += self.embed_positions(src_tokens)
        # language embedding
        if self.embed_language is not None:
            lang_emb = self.embed_scale * self.embed_language.unsqueeze_(1)
            x += lang_emb
        x = F.dropout(x, p=self.dropout, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        # compute padding mask
        encoder_padding_mask = src_tokens.eq(self.padding_idx)
        if not encoder_padding_mask.any():
            encoder_padding_mask = None

        return {
            'encoder_out': x,  # T x B x C
            'encoder_padding_mask': encoder_padding_mask,  # B x T
        }

    def reorder_encoder_out(self, encoder_out, new_order):
        """
        Reorder encoder output according to *new_order*.

        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order

        Returns:
            *encoder_out* rearranged according to *new_order*
        """
        if encoder_out['encoder_out'] is not None:
            encoder_out['encoder_out'] = \
                encoder_out['encoder_out'].index_select(1, new_order)
        if encoder_out['encoder_padding_mask'] is not None:
            encoder_out['encoder_padding_mask'] = \
                encoder_out['encoder_padding_mask'].index_select(0, new_order)
        return encoder_out

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        if self.embed_positions is None:
            return self.max_source_positions
        return min(self.max_source_positions, self.embed_positions.max_positions())


class JointSourceTargetDecoder(FairseqIncrementalDecoder):
    """
    JointSourceTarge decoder consisting of *args.decoder_layers* layers. Each layer
    is a :class:`TransformerDecoderLayer`.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): decoding dictionary
        embed_tokens (torch.nn.Embedding): output embedding
        left_pad (bool, optional): whether the input is left-padded. Default:
            ``False``
    """

    def __init__(self, args, dictionary, embed_tokens, left_pad=False, final_norm=True):
        super().__init__(dictionary)
        self.dropout = args.dropout
        self.share_input_output_embed = args.share_decoder_input_output_embed
        self.transformer_kernel_size_list = getattr(args, 'transformer_kernel_size_list', None)

        input_embed_dim = embed_tokens.embedding_dim
        embed_dim = args.decoder_embed_dim
        output_embed_dim = args.decoder_output_dim

        padding_idx = embed_tokens.padding_idx
        self.max_target_positions = args.max_target_positions

        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(embed_dim)  # todo: try with input_embed_dim

        self.project_in_dim = Linear(input_embed_dim, embed_dim, bias=False) if embed_dim != input_embed_dim else None

        self.embed_positions = PositionalEmbedding(
            args.max_target_positions, embed_dim, padding_idx,
            left_pad=left_pad,
            learned=args.decoder_learned_pos,
        ) if not args.no_token_positional_embeddings else None

        self.embed_language = LanguageEmbedding(embed_dim) if args.language_embeddings else None

        self.layers = nn.ModuleList([])
        self.layers.extend([
            TransformerDecoderLayer(args, no_encoder_attn=True)
            for _ in range(args.decoder_layers)
        ])

        self.adaptive_softmax = None

        self.project_out_dim = Linear(embed_dim, output_embed_dim, bias=False) \
            if embed_dim != output_embed_dim and not args.tie_adaptive_weights else None

        if args.adaptive_softmax_cutoff is not None:
            self.adaptive_softmax = AdaptiveSoftmax(
                len(dictionary),
                output_embed_dim,
                options.eval_str_list(args.adaptive_softmax_cutoff, type=int),
                dropout=args.adaptive_softmax_dropout,
                adaptive_inputs=embed_tokens if args.tie_adaptive_weights else None,
                factor=args.adaptive_softmax_factor,
                tie_proj=args.tie_adaptive_proj,
            )
        elif not self.share_input_output_embed:
            self.embed_out = nn.Parameter(torch.Tensor(len(dictionary), output_embed_dim))
            nn.init.normal_(self.embed_out, mean=0, std=output_embed_dim ** -0.5)
        self.register_buffer('version', torch.Tensor([2]))
        self.normalize = args.decoder_normalize_before and final_norm
        if self.normalize:
            self.layer_norm = LayerNorm(embed_dim)

    def forward(self, input, encoder_out=None, incremental_state=None):
        """
        Args:
            input (dict): with
                prev_output_tokens (LongTensor): previous decoder outputs of shape
                    `(batch, tgt_len)`, for input feeding/teacher forcing
            encoder_out (Tensor, optional): output from the encoder, used for
                encoder-side attention
            incremental_state (dict): dictionary used for storing state during
                :ref:`Incremental decoding`

        Returns:
            tuple:
                - the last decoder layer's output of shape `(batch, tgt_len,
                  vocab)`
                - the last decoder layer's attention weights of shape `(batch,
                  tgt_len, src_len)`
        """
        prev_output_tokens = input['prev_output_tokens']
        tgt_len = prev_output_tokens.size(1)

        # embed positions
        positions = self.embed_positions(
            prev_output_tokens,
            incremental_state=incremental_state,
        ) if self.embed_positions is not None else None

        if incremental_state is not None:
            prev_output_tokens = prev_output_tokens[:, -1:]
            if positions is not None:
                positions = positions[:, -1:]

        # embed tokens and positions
        x = self.embed_scale * self.embed_tokens(prev_output_tokens)

        if self.project_in_dim is not None:
            x = self.project_in_dim(x)

        if positions is not None:
            x += positions

        # language embedding
        if self.embed_language is not None:
            lang_emb = self.embed_scale * self.embed_language.unsqueeze_(1)
            x += lang_emb

        x = F.dropout(x, p=self.dropout, training=self.training)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)
        attn = None
        process_source = self.mixed_attention and (incremental_state is None or len(incremental_state) == 0)

        inner_states = [x]

        # target only decoder layers
        if not self.interleaved:
            for layer in self.target_layers:
                x, attn = layer(
                    x,
                    None,
                    None,
                    incremental_state
                )
                inner_states.append(x)

        # source only decoder layers
        source = encoder_out['encoder_out']
        if process_source:
            if not self.interleaved:
                for layer in self.source_layers:
                    source, attn = layer(
                        source,
                        encoder_out['encoder_padding_mask']
                    )
                    inner_states.append(source)

        # extended padding mask
        source_padding_mask = encoder_out['encoder_padding_mask'] if self.mixed_attention else None
        if source_padding_mask is not None:
            target_padding_mask = source_padding_mask.new_zeros((source_padding_mask.size(0), tgt_len))
            self_attn_padding_mask = torch.cat((source_padding_mask, target_padding_mask), dim=1)
        else:
            self_attn_padding_mask = None

        # shared transformer layers
        if len(self.transformer_layers) > 0:
            for i, layer in enumerate(self.transformer_layers):
                if self.interleaved:
                    if process_source:
                        if i < len(self.source_layers):
                            source = self.source_layers[i](
                                source,
                                encoder_out['encoder_padding_mask']
                            )
                            inner_states.append(source)
                    if i < len(self.target_layers):
                        x, attn = self.target_layers[i](
                            x,
                            None,
                            None,
                            incremental_state
                        )
                        inner_states.append(x)  

                if self.transformer_kernel_size_list is not None:
                    target_mask = self.local_mask(x, self.transformer_kernel_size_list[i], causal=True, tgt_len=tgt_len)
                elif incremental_state is None:
                    target_mask = self.buffered_future_mask(x)
                else:
                    target_mask = None

                if target_mask is not None and self.mixed_attention:
                    zero_mask = target_mask.new_zeros((target_mask.size(0), source.size(0)))
                    self_attn_mask = torch.cat((zero_mask, target_mask), dim=1)
                else:
                    self_attn_mask = target_mask

                state = incremental_state
                if process_source:
                    if state is None:
                        state = {}
                    if self.transformer_kernel_size_list is not None:
                        source_mask = self.local_mask(source, self.transformer_kernel_size_list[i], causal=False)
                    else:
                        source_mask = None
                    source, attn = layer(
                        source,
                        None,
                        None,
                        state,
                        self_attn_mask=source_mask,
                        self_attn_padding_mask=source_padding_mask
                    )
                    inner_states.append(source)

                x, attn = layer(
                    x,
                    encoder_out['encoder_out'] if encoder_out is not None and not self.mixed_attention else None,
                    encoder_out['encoder_padding_mask'] if encoder_out is not None and not self.mixed_attention else None,
                    state,
                    self_attn_mask=self_attn_mask,
                    self_attn_padding_mask=self_attn_padding_mask
                )
                inner_states.append(x)

        # conv layers
        for layer in self.layers:
            x, attn = layer(
                x,
                encoder_out['encoder_out'] if encoder_out is not None and not self.mixed_attention else None,
                encoder_out['encoder_padding_mask'] if encoder_out is not None and not self.mixed_attention else None,
                incremental_state,
            )
            inner_states.append(x)

        if self.normalize:
            x = self.layer_norm(x)

        # T x B x C -> B x T x C
        x = x.transpose(0, 1)

        if self.project_out_dim is not None:
            x = self.project_out_dim(x)

        if self.adaptive_softmax is None:
            # project back to size of vocabulary
            if self.share_input_output_embed:
                x = F.linear(x, self.embed_tokens.weight)
            else:
                x = F.linear(x, self.embed_out)

        pred = x
        info = {'attn': attn, 'inner_states': inner_states}

        return pred, info

    def max_positions(self):
        """Maximum output length supported by the decoder."""
        if self.embed_positions is None:
            return self.max_target_positions
        return min(self.max_target_positions, self.embed_positions.max_positions())

    def buffered_future_mask(self, tensor):
        dim = tensor.size(0)
        if not hasattr(self, '_future_mask') or self._future_mask is None or self._future_mask.device != tensor.device:
            self._future_mask = torch.triu(utils.fill_with_neg_inf(tensor.new(dim, dim)), 1)
        if self._future_mask.size(0) < dim:
            self._future_mask = torch.triu(utils.fill_with_neg_inf(self._future_mask.resize_(dim, dim)), 1)
        return self._future_mask[:dim, :dim]

    def local_mask(self, tensor, kernel_size, causal, tgt_len=None):
        rows = tensor.size(0)
        cols = tensor.size(0) if tgt_len is None else tgt_len
        if causal:
            if rows == 1:
                mask = utils.fill_with_neg_inf(tensor.new(1, cols))
                mask[0, -kernel_size:] = 0
                return mask
            else:
                diag_u, diag_l = 1, kernel_size
        else:
            diag_u, diag_l = ((kernel_size + 1) // 2, (kernel_size + 1) // 2) if kernel_size % 2 == 1 else (kernel_size // 2, kernel_size // 2 + 1)
        mask1 = torch.triu(utils.fill_with_neg_inf(tensor.new(rows, cols)), diag_u)
        mask2 = torch.tril(utils.fill_with_neg_inf(tensor.new(rows, cols)), -diag_l)

        return mask1 + mask2


class LightConvEncoderLayer(nn.Module):
    """Encoder layer block.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        kernel_size: kernel size of the convolution
    """

    def __init__(self, args, kernel_size=0):
        super().__init__()
        self.embed_dim = args.encoder_embed_dim
        self.conv_dim = args.encoder_conv_dim
        padding_l = kernel_size // 2 if kernel_size % 2 == 1 else ((kernel_size - 1) // 2, kernel_size // 2)

        if args.encoder_glu:
            self.linear1 = Linear(self.embed_dim, 2*self.conv_dim)
            self.act = nn.GLU()
        else:
            self.linear1 = Linear(self.embed_dim, self.conv_dim)
            self.act = None
        if args.encoder_conv_type == 'lightweight':
            self.conv = LightweightConv1dTBC(self.conv_dim, kernel_size, padding_l=padding_l,
                                             weight_softmax=args.weight_softmax,
                                             num_heads=args.encoder_attention_heads,
                                             weight_dropout=args.weight_dropout)
        elif args.encoder_conv_type == 'dynamic':
            self.conv = DynamicConv1dTBC(self.conv_dim, kernel_size, padding_l=padding_l,
                                         weight_softmax=args.weight_softmax,
                                         num_heads=args.encoder_attention_heads,
                                         weight_dropout=args.weight_dropout)
        else:
            raise NotImplementedError
        self.linear2 = Linear(self.conv_dim, self.embed_dim)

        self.dropout = args.dropout
        self.relu_dropout = args.relu_dropout
        self.input_dropout = args.input_dropout
        self.normalize_before = args.encoder_normalize_before
        self.fc1 = Linear(self.embed_dim, args.encoder_ffn_embed_dim)
        self.fc2 = Linear(args.encoder_ffn_embed_dim, self.embed_dim)
        self.layer_norms = nn.ModuleList([LayerNorm(self.embed_dim) for _ in range(2)])

    def forward(self, x, encoder_padding_mask):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor): binary ByteTensor of shape
                `(batch, src_len)` where padding elements are indicated by ``1``.

        Returns:
            encoded output of shape `(batch, src_len, embed_dim)`
        """
        residual = x
        x = self.maybe_layer_norm(0, x, before=True)
        x = F.dropout(x, p=self.input_dropout, training=self.training)
        x = self.linear1(x)
        if self.act is not None:
            x = self.act(x)
        if encoder_padding_mask is not None:
            x = x.masked_fill(encoder_padding_mask.transpose(0, 1).unsqueeze(2), 0)
        x = self.conv(x)
        x = self.linear2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(0, x, after=True)

        residual = x
        x = self.maybe_layer_norm(1, x, before=True)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, p=self.relu_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(1, x, after=True)
        return x

    def maybe_layer_norm(self, i, x, before=False, after=False):
        assert before ^ after
        if after ^ self.normalize_before:
            return self.layer_norms[i](x)
        else:
            return x

    def extra_repr(self):
        return 'dropout={}, relu_dropout={}, input_dropout={}, normalize_before={}'.format(
            self.dropout, self.relu_dropout, self.input_dropout, self.normalize_before)


class LightConvDecoderLayer(nn.Module):
    """Decoder layer block.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        no_encoder_attn (bool, optional): whether to attend to encoder outputs.
            Default: ``False``
        kernel_size: kernel size of the convolution
    """

    def __init__(self, args, no_encoder_attn=False, kernel_size=0):
        super().__init__()
        self.embed_dim = args.decoder_embed_dim
        self.conv_dim = args.decoder_conv_dim
        if args.decoder_glu:
            self.linear1 = Linear(self.embed_dim, 2*self.conv_dim)
            self.act = nn.GLU()
        else:
            self.linear1 = Linear(self.embed_dim, self.conv_dim)
            self.act = None
        if args.decoder_conv_type == 'lightweight':
            self.conv = LightweightConv1dTBC(self.conv_dim, kernel_size, padding_l=kernel_size-1,
                                             weight_softmax=args.weight_softmax,
                                             num_heads=args.decoder_attention_heads,
                                             weight_dropout=args.weight_dropout)
        elif args.decoder_conv_type == 'dynamic':
            self.conv = DynamicConv1dTBC(self.conv_dim, kernel_size, padding_l=kernel_size-1,
                                         weight_softmax=args.weight_softmax,
                                         num_heads=args.decoder_attention_heads,
                                         weight_dropout=args.weight_dropout)
        else:
            raise NotImplementedError
        self.linear2 = Linear(self.conv_dim, self.embed_dim)

        self.dropout = args.dropout
        self.relu_dropout = args.relu_dropout
        self.input_dropout = args.input_dropout
        self.normalize_before = args.decoder_normalize_before

        self.conv_layer_norm = LayerNorm(self.embed_dim)

        if no_encoder_attn:
            self.encoder_attn = None
            self.encoder_attn_layer_norm = None
        else:
            self.encoder_attn = MultiheadAttention(
                self.embed_dim, args.decoder_attention_heads,
                dropout=args.attention_dropout,
            )
            self.encoder_attn_layer_norm = LayerNorm(self.embed_dim)

        self.fc1 = Linear(self.embed_dim, args.decoder_ffn_embed_dim)
        self.fc2 = Linear(args.decoder_ffn_embed_dim, self.embed_dim)

        self.final_layer_norm = LayerNorm(self.embed_dim)
        self.need_attn = True

    def forward(self, x, encoder_out, encoder_padding_mask, incremental_state,
                prev_conv_state=None, prev_attn_state=None, conv_mask=None,
                conv_padding_mask=None):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor): binary ByteTensor of shape
                `(batch, src_len)` where padding elements are indicated by ``1``.

        Returns:
            encoded output of shape `(batch, src_len, embed_dim)`
        """
        residual = x
        x = self.maybe_layer_norm(self.conv_layer_norm, x, before=True)
        if prev_conv_state is not None:
            if incremental_state is None:
                incremental_state = {}
            self.conv._set_input_buffer(incremental_state, prev_conv_state)
        x = F.dropout(x, p=self.input_dropout, training=self.training)
        x = self.linear1(x)
        if self.act is not None:
            x = self.act(x)
        x = self.conv(x, incremental_state=incremental_state)
        x = self.linear2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(self.conv_layer_norm, x, after=True)

        attn = None
        if self.encoder_attn is not None:
            residual = x
            x = self.maybe_layer_norm(self.encoder_attn_layer_norm, x, before=True)
            if prev_attn_state is not None:
                if incremental_state is None:
                    incremental_state = {}
                prev_key, prev_value = prev_attn_state
                saved_state = {"prev_key": prev_key, "prev_value": prev_value}
                self.encoder_attn._set_input_buffer(incremental_state, saved_state)
            x, attn = self.encoder_attn(
                query=x,
                key=encoder_out,
                value=encoder_out,
                key_padding_mask=encoder_padding_mask,
                incremental_state=incremental_state,
                static_kv=True,
                need_weights=(not self.training and self.need_attn),
            )
            x = F.dropout(x, p=self.dropout, training=self.training)
            x = residual + x
            x = self.maybe_layer_norm(self.encoder_attn_layer_norm, x, after=True)

        residual = x
        x = self.maybe_layer_norm(self.final_layer_norm, x, before=True)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, p=self.relu_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(self.final_layer_norm, x, after=True)
        return x, attn

    def maybe_layer_norm(self, layer_norm, x, before=False, after=False):
        assert before ^ after
        if after ^ self.normalize_before:
            return layer_norm(x)
        else:
            return x

    def make_generation_fast_(self, need_attn=False, **kwargs):
        self.need_attn = need_attn

    def extra_repr(self):
        return 'dropout={}, relu_dropout={}, input_dropout={}, normalize_before={}'.format(
            self.dropout, self.relu_dropout, self.input_dropout, self.normalize_before)


def Embedding(num_embeddings, embedding_dim, padding_idx):
    m = nn.Embedding(num_embeddings, embedding_dim, padding_idx=padding_idx)
    nn.init.normal_(m.weight, mean=0, std=embedding_dim ** -0.5)
    nn.init.constant_(m.weight[padding_idx], 0)
    return m


def LanguageEmbedding(embedding_dim)
    m = Parameter(torch.Tensor(embedding_dim))
    nn.init.normal_(m.weight, mean=0, std=embedding_dim ** -0.5)
    return m
}


def LayerNorm(embedding_dim):
    m = nn.LayerNorm(embedding_dim)
    return m


def Linear(in_features, out_features, bias=True):
    m = nn.Linear(in_features, out_features, bias)
    nn.init.xavier_uniform_(m.weight)
    if bias:
        nn.init.constant_(m.bias, 0.)
    return m


def PositionalEmbedding(num_embeddings, embedding_dim, padding_idx, left_pad, learned=False):
    if learned:
        m = LearnedPositionalEmbedding(num_embeddings + padding_idx + 1, embedding_dim, padding_idx, left_pad)
        nn.init.normal_(m.weight, mean=0, std=embedding_dim ** -0.5)
        nn.init.constant_(m.weight[padding_idx], 0)
    else:
        m = SinusoidalPositionalEmbedding(embedding_dim, padding_idx, left_pad, num_embeddings + padding_idx + 1)
    return m


@register_model_architecture('lightconv_lm', 'lightconv_lm')
def base_lm_architecture(args):
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 512)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 2048)
    args.decoder_layers = getattr(args, 'decoder_layers', 6)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 8)
    args.adaptive_softmax_cutoff = getattr(args, 'adaptive_softmax_cutoff', None)
    args.adaptive_softmax_dropout = getattr(args, 'adaptive_softmax_dropout', 0)
    args.adaptive_softmax_factor = getattr(args, 'adaptive_softmax_factor', 4)
    args.decoder_learned_pos = getattr(args, 'decoder_learned_pos', False)

    args.character_embeddings = getattr(args, 'character_embeddings', False)

    args.decoder_output_dim = getattr(args, 'decoder_output_dim', args.decoder_embed_dim)
    args.decoder_input_dim = getattr(args, 'decoder_input_dim', args.decoder_embed_dim)

    # The model training is not stable without this
    args.decoder_normalize_before = True

    args.adaptive_input = getattr(args, 'adaptive_input', False)
    args.adaptive_input_factor = getattr(args, 'adaptive_input_factor', 4)
    args.adaptive_input_cutoff = getattr(args, 'adaptive_input_cutoff', None)

    args.tie_adaptive_weights = getattr(args, 'tie_adaptive_weights', False)
    args.tie_adaptive_proj = getattr(args, 'tie_adaptive_proj', False)

    args.decoder_kernel_size_list = getattr(args, 'decoder_kernel_size_list', [3, 7, 15, 31, 31, 31])
    if len(args.decoder_kernel_size_list) == 1:
        args.decoder_kernel_size_list = args.decoder_kernel_size_list * args.decoder_layers


@register_model_architecture('lightconv_lm', 'lightconv_lm_gbw')
def lightconv_lm_gbw(args):
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 512)
    args.dropout = getattr(args, 'dropout', 0.1)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 4096)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 16)
    base_lm_architecture(args)


@register_model_architecture('lightconv', 'lightconv')
def base_architecture(args):
    args.encoder_embed_path = getattr(args, 'encoder_embed_path', None)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 512)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 2048)
    args.encoder_layers = getattr(args, 'encoder_layers', 7)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 8)
    args.encoder_normalize_before = getattr(args, 'encoder_normalize_before', False)
    args.encoder_learned_pos = getattr(args, 'encoder_learned_pos', False)
    args.decoder_embed_path = getattr(args, 'decoder_embed_path', None)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', args.encoder_ffn_embed_dim)
    args.decoder_layers = getattr(args, 'decoder_layers', 6)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 8)
    args.decoder_normalize_before = getattr(args, 'decoder_normalize_before', False)
    args.decoder_learned_pos = getattr(args, 'decoder_learned_pos', False)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.)
    args.relu_dropout = getattr(args, 'relu_dropout', 0.)
    args.dropout = getattr(args, 'dropout', 0.1)
    args.adaptive_softmax_cutoff = getattr(args, 'adaptive_softmax_cutoff', None)
    args.adaptive_softmax_dropout = getattr(args, 'adaptive_softmax_dropout', 0)
    args.share_decoder_input_output_embed = getattr(args, 'share_decoder_input_output_embed', False)
    args.share_all_embeddings = getattr(args, 'share_all_embeddings', False)
    args.no_token_positional_embeddings = getattr(args, 'no_token_positional_embeddings', False)

    args.decoder_output_dim = getattr(args, 'decoder_output_dim', args.decoder_embed_dim)
    args.decoder_input_dim = getattr(args, 'decoder_input_dim', args.decoder_embed_dim)

    args.encoder_conv_dim = getattr(args, 'encoder_conv_dim', args.encoder_embed_dim)
    args.decoder_conv_dim = getattr(args, 'decoder_conv_dim', args.decoder_embed_dim)

    args.encoder_kernel_size_list = getattr(args, 'encoder_kernel_size_list', [3, 7, 15, 31, 31, 31, 31])
    args.decoder_kernel_size_list = getattr(args, 'decoder_kernel_size_list', [3, 7, 15, 31, 31, 31])
    if len(args.encoder_kernel_size_list) == 1:
        args.encoder_kernel_size_list = args.encoder_kernel_size_list * args.encoder_layers
    if len(args.decoder_kernel_size_list) == 1:
        args.decoder_kernel_size_list = args.decoder_kernel_size_list * args.decoder_layers
    assert len(args.encoder_kernel_size_list) == args.encoder_layers, "encoder_kernel_size_list doesn't match encoder_layers"
    assert len(args.decoder_kernel_size_list) == args.decoder_layers, "decoder_kernel_size_list doesn't match decoder_layers"
    args.encoder_glu = getattr(args, 'encoder_glu', True)
    args.decoder_glu = getattr(args, 'decoder_glu', True)
    args.input_dropout = getattr(args, 'input_dropout', 0.1)
    args.weight_dropout = getattr(args, 'weight_dropout', args.attention_dropout)

    args.decoder_source_layers = getattr(args, 'decoder_source_layers', 0)
    args.decoder_target_layers = getattr(args, 'decoder_target_layers', 0)
    args.decoder_transformer_layers = getattr(args, 'decoder_transformer_layers', 0)
    args.mixed_attention = getattr(args, 'mixed_attention', False)
    args.language_embeddings = getattr(args, 'language_embeddings', False)
    args.language_embed_path = getattr(args, 'language_embed_path', None)
    args.extra_inputs = getattr(args, 'extra_inputs', 0)


@register_model_architecture('lightconv', 'lightconv_iwslt_de_en')
def lightconv_iwslt_de_en(args):
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 512)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 1024)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 4)
    args.encoder_layers = getattr(args, 'encoder_layers', 7)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 512)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 1024)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 4)
    args.decoder_layers = getattr(args, 'decoder_layers', 6)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.weight_dropout = getattr(args, 'weight_dropout', 0.1)
    args.encoder_glu = getattr(args, 'encoder_glu', False)
    args.decoder_glu = getattr(args, 'decoder_glu', False)
    args.input_dropout = getattr(args, 'input_dropout', 0.0)
    base_architecture(args)


@register_model_architecture('lightconv', 'mixed_attention_iwslt_de_en')
def mixed_attention_iwslt_de_en(args):
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 256)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 1024)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 4)
    args.encoder_layers = getattr(args, 'encoder_layers', 0)
    args.encoder_kernel_size_list = getattr(args, 'encoder_kernel_size_list', [])
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 256)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 1024)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 4)
    args.decoder_layers = getattr(args, 'decoder_layers', 0)
    args.decoder_kernel_size_list = getattr(args, 'decoder_kernel_size_list', [])

    args.interleaved = getattr(args, 'interleaved', False)
    args.decoder_source_layers = getattr(args, 'decoder_source_layers', 0)
    args.decoder_source_kernel_size_list = getattr(args, 'decoder_source_kernel_size_list', [])
    args.decoder_target_layers = getattr(args, 'decoder_target_layers', 0)
    args.decoder_target_kernel_size_list = getattr(args, 'decoder_target_kernel_size_list', [])
    args.decoder_transformer_layers = getattr(args, 'decoder_transformer_layers', 14)
    args.decoder_transformer_kernel_size_list = getattr(args, 'decoder_transformer_kernel_size_list', None)
    args.mixed_attention = getattr(args, 'mixed_attention', True)
    args.language_embeddings = getattr(args, 'language_embeddings', True)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.weight_dropout = getattr(args, 'weight_dropout', 0.1)
    args.encoder_glu = getattr(args, 'encoder_glu', False)
    args.decoder_glu = getattr(args, 'decoder_glu', False)
    args.input_dropout = getattr(args, 'input_dropout', 0.0)
    base_architecture(args)


@register_model_architecture('lightconv', 'local_attention_iwslt_de_en')
def local_attention_iwslt_de_en(args):
    args.decoder_transformer_kernel_size_list = getattr(args, 'decoder_transformer_kernel_size_list', [3, 5, 7, 9, 11, 13, 15, 17, 21, 25, 29, 33, 37, 41])
    mixed_attention_iwslt_de_en(args)


@register_model_architecture('lightconv', 'lightconv_wmt_en_de')
def lightconv_wmt_en_de(args):
    base_architecture(args)


@register_model_architecture('lightconv', 'lightconv_wmt_en_de_big')
def lightconv_wmt_en_de_big(args):
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 1024)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 4096)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 16)
    args.encoder_normalize_before = getattr(args, 'encoder_normalize_before', False)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 1024)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 4096)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 16)
    args.dropout = getattr(args, 'dropout', 0.3)
    base_architecture(args)


@register_model_architecture('lightconv', 'mixed_attention_wmt_en_de_big')
def mixed_attention_wmt_en_de_big(args):
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 1024)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 4096)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 16)
    args.encoder_layers = getattr(args, 'encoder_layers', 0)
    args.encoder_kernel_size_list = getattr(args, 'encoder_kernel_size_list', [])
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 1024)
    args.decoder_ffn_embed_dim = getattr(args, 'decoder_ffn_embed_dim', 4096)
    args.decoder_attention_heads = getattr(args, 'decoder_attention_heads', 16)
    args.decoder_layers = getattr(args, 'decoder_layers', 0)
    args.decoder_kernel_size_list = getattr(args, 'decoder_kernel_size_list', [])

    args.mixed_attention = getattr(args, 'mixed_attention', True)
    args.interleaved = getattr(args, 'interleaved', False)
    args.decoder_source_layers = getattr(args, 'decoder_source_layers', 0)
    args.decoder_source_kernel_size_list = getattr(args, 'decoder_source_kernel_size_list', [])
    args.decoder_target_layers = getattr(args, 'decoder_target_layers', 0)
    args.decoder_target_kernel_size_list = getattr(args, 'decoder_target_kernel_size_list', [])
    args.decoder_transformer_layers = getattr(args, 'decoder_transformer_layers', 14)
    args.decoder_transformer_kernel_size_list = getattr(args, 'decoder_transformer_kernel_size_list', None)
    args.language_embeddings = getattr(args, 'language_embeddings', True)
    args.dropout = getattr(args, 'dropout', 0.3)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.weight_dropout = getattr(args, 'weight_dropout', 0.1)
    base_architecture(args)


@register_model_architecture('lightconv', 'lightconv_wmt_en_fr_big')
def lightconv_wmt_en_fr_big(args):
    args.dropout = getattr(args, 'dropout', 0.1)
    lightconv_wmt_en_de_big(args)


@register_model_architecture('lightconv', 'mixed_attention_wmt_en_fr_big')
def mixed_attention_wmt_en_fr_big(args):
    args.dropout = getattr(args, 'dropout', 0.1)
    mixed_attention_wmt_en_de_big(args)


@register_model_architecture('lightconv', 'lightconv_wmt_zh_en_big')
def lightconv_wmt_zh_en_big(args):
    args.dropout = getattr(args, 'dropout', 0.2)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.2)
    args.weight_dropout = getattr(args, 'weight_dropout', 0.2)
    lightconv_wmt_en_de_big(args)
