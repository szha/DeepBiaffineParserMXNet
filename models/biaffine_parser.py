# -*- coding: UTF-8 -*-

import mxnet as mx
import numpy as np
from mxnet import nd
from mxnet.gluon import nn
from mxnet.gluon.loss import SoftmaxCrossEntropyLoss

from common.utils import biLSTM, leaky_relu, bilinear, orthonormal_initializer, arc_argmax, rel_argmax, \
    orthonormal_VanillaBiLSTMBuilder, reshape_fortran


class BiaffineParser(nn.Block):
    def __init__(self, vocab,
                 word_dims,
                 tag_dims,
                 dropout_dim,
                 lstm_layers,
                 lstm_hiddens,
                 dropout_lstm_input,
                 dropout_lstm_hidden,
                 mlp_arc_size,
                 mlp_rel_size,
                 dropout_mlp,
                 debug=False
                 ):
        super(BiaffineParser, self).__init__()

        def embedding_from_numpy(_we):
            word_embs = nn.Embedding(_we.shape[0], _we.shape[1], weight_initializer=mx.init.Constant(_we))
            return word_embs

        self._vocab = vocab
        self.word_embs = embedding_from_numpy(vocab.get_word_embs(word_dims))
        self.pret_word_embs = embedding_from_numpy(vocab.get_pret_embs()) if vocab.has_pret_embs() else None
        self.tag_embs = embedding_from_numpy(vocab.get_tag_embs(tag_dims))

        self.bi_lstm = nn.Sequential()
        self.bi_lstm.add(orthonormal_VanillaBiLSTMBuilder(1, word_dims + tag_dims, lstm_hiddens, dropout_lstm_input,
                                                          dropout_lstm_hidden, debug))
        for i in range(lstm_layers - 1):
            self.bi_lstm.add(orthonormal_VanillaBiLSTMBuilder(1, 2 * lstm_hiddens, lstm_hiddens, dropout_lstm_input,
                                                              dropout_lstm_hidden, debug))
        self.dropout_lstm_input = dropout_lstm_input
        self.dropout_lstm_hidden = dropout_lstm_hidden

        mlp_size = mlp_arc_size + mlp_rel_size
        W = orthonormal_initializer(mlp_size, 2 * lstm_hiddens, debug)
        self.mlp_dep_W = self.parameter_from_numpy('mlp_dep_W', W)
        self.mlp_head_W = self.parameter_from_numpy('mlp_head_W', W)
        self.mlp_dep_b = self.parameter_init('mlp_dep_b', (mlp_size,), mx.init.Zero())
        self.mlp_head_b = self.parameter_init('mlp_head_b', (mlp_size,), mx.init.Zero())
        self.mlp_arc_size = mlp_arc_size
        self.mlp_rel_size = mlp_rel_size
        self.dropout_mlp = dropout_mlp

        self.arc_W = self.parameter_init('arc_W', (mlp_arc_size, mlp_arc_size + 1), init=mx.init.Zero())
        self.rel_W = self.parameter_init('rel_W', (vocab.rel_size * (mlp_rel_size + 1), mlp_rel_size + 1),
                                         init=mx.init.Zero())
        self.softmax_loss = SoftmaxCrossEntropyLoss(axis=0, batch_axis=-1)
        # for k, v in self.collect_params().items():
        #     print(k)
        #     v.initialize(ctx=mx.cpu())

        self.initialize()

        def _emb_mask_generator(seq_len, batch_size):
            wm, tm = nd.zeros((seq_len, batch_size, 1)), nd.zeros((seq_len, batch_size, 1))
            for i in range(seq_len):
                word_mask = np.random.binomial(1, 1. - dropout_dim, batch_size).astype(np.float32)
                tag_mask = np.random.binomial(1, 1. - dropout_dim, batch_size).astype(np.float32)
                scale = 3. / (2. * word_mask + tag_mask + 1e-12)
                word_mask *= scale
                tag_mask *= scale
                word_mask = nd.array(word_mask)
                tag_mask = nd.array(tag_mask)
                wm[i, :, 0] = word_mask
                tm[i, :, 0] = tag_mask
            return wm, tm

        self.generate_emb_mask = _emb_mask_generator

    def parameter_from_numpy(self, name, array):
        p = self.params.get(name, shape=array.shape, init=mx.init.Constant(array))
        return p
        # p.initialize()
        # return p.data()

    def parameter_init(self, name, shape, init):
        p = self.params.get(name, shape=shape, init=init)
        return p
        # p.initialize()
        # return p.data()

    def forward(self, *args):
        pass

    @property
    def parameter_collection(self):
        return self._pc

    def run(self, word_inputs, tag_inputs, arc_targets=None, rel_targets=None, is_train=True):
        """
        Train or test
        :param word_inputs: seq_len x batch_size
        :param tag_inputs: seq_len x batch_size
        :param arc_targets: seq_len x batch_size
        :param rel_targets: seq_len x batch_size
        :param is_train: is training or test
        :return:
        """

        # return 0, 0, 0, nd.dot(self.junk.data(), nd.ones((3, 1))).sum()

        def flatten_numpy(ndarray):
            """
            Flatten nd-array to 1-d column vector
            :param ndarray:
            :return:
            """
            return np.reshape(ndarray, (-1,), 'F')

        batch_size = word_inputs.shape[1]
        seq_len = word_inputs.shape[0]
        mask = np.greater(word_inputs, self._vocab.ROOT).astype(np.float32)
        num_tokens = int(np.sum(mask))  # non padding, non root token number

        if is_train or arc_targets is not None:
            mask_1D = flatten_numpy(mask)
            # mask_1D_tensor = nd.inputTensor(mask_1D, batched=True)
            mask_1D_tensor = nd.array(mask_1D)

            #  if batched=True, the last dimension is used as a batch dimension if arr is a list of numpy ndarrays

        unked_words = np.where(word_inputs < self._vocab.words_in_train, word_inputs, self._vocab.UNK)
        word_embs = self.word_embs(nd.array(unked_words, dtype='int'))
        if self.pret_word_embs:
            word_embs = word_embs + self.pret_word_embs(nd.array(word_inputs))
        tag_embs = self.tag_embs(nd.array(tag_inputs))

        # Dropout
        if is_train:
            wm, tm = self.generate_emb_mask(seq_len, batch_size)
            emb_inputs = nd.concat(nd.multiply(wm, word_embs), nd.multiply(tm, tag_embs), dim=2)
        else:
            emb_inputs = nd.concat(word_embs, tag_embs, dim=2)  # seq_len x batch_size

        top_recur = biLSTM(self.bi_lstm, emb_inputs, batch_size)
        # if is_train:
        #     top_recur = nd.Dropout(data=top_recur, axes=[0], p=self.dropout_mlp)

        W_dep, b_dep = self.mlp_dep_W.data(), self.mlp_dep_b.data()
        W_head, b_head = self.mlp_head_W.data(), self.mlp_head_b.data()
        dep, head = leaky_relu(nd.dot(top_recur, W_dep.T) + b_dep), leaky_relu(nd.dot(top_recur, W_head.T) + b_head)
        # if is_train:
        #     dep, head = nd.Dropout(data=dep, axes=[0], p=self.dropout_mlp), nd.Dropout(data=head, axes=[0],
        #                                                                                p=self.dropout_mlp)
        dep, head = nd.transpose(dep, axes=[2, 0, 1]), nd.transpose(head, axes=[2, 0, 1])
        dep_arc, dep_rel = dep[:self.mlp_arc_size], dep[self.mlp_arc_size:]
        head_arc, head_rel = head[:self.mlp_arc_size], head[self.mlp_arc_size:]
        # return 0, 0, 0, dep_arc.sum() + head_arc.sum()

        W_arc = self.arc_W.data()
        arc_logits = bilinear(dep_arc, W_arc, head_arc, self.mlp_arc_size, seq_len, batch_size, num_outputs=1,
                              bias_x=True, bias_y=False)
        # return 0, 0, 0, arc_logits.sum()
        # (#head x #dep) x batch_size

        flat_arc_logits = reshape_fortran(arc_logits, (seq_len, seq_len * batch_size))
        # (#head ) x (#dep x batch_size)

        arc_preds = arc_logits.argmax(0)
        if len(arc_preds.shape) == 1:  # dynet did unnecessary jobs
            arc_preds = np.expand_dims(arc_preds, axis=1)
        # seq_len x batch_size

        if is_train or arc_targets is not None:
            correct = np.equal(arc_preds.asnumpy(), arc_targets)
            arc_correct = correct.astype(np.float32) * mask
            arc_accuracy = np.sum(arc_correct) / num_tokens
            targets_1D = flatten_numpy(arc_targets)
            losses = self.softmax_loss(flat_arc_logits, nd.array(targets_1D))
            arc_loss = nd.sum(losses * mask_1D_tensor) / num_tokens

        if not is_train:
            arc_probs = np.transpose(
                np.reshape(nd.softmax(flat_arc_logits).asnumpy(), (seq_len, seq_len, batch_size), 'F'))
        # #batch_size x #dep x #head

        W_rel = self.rel_W.data()
        # dep_rel = nd.concat([dep_rel, nd.inputTensor(np.ones((1, seq_len),dtype=np.float32))])
        # head_rel = nd.concat([head_rel, nd.inputTensor(np.ones((1, seq_len), dtype=np.float32))])
        rel_logits = bilinear(dep_rel, W_rel, head_rel, self.mlp_rel_size, seq_len, batch_size,
                              num_outputs=self._vocab.rel_size, bias_x=True, bias_y=True)
        # (#head x rel_size x #dep) x batch_size

        flat_rel_logits = reshape_fortran(rel_logits, (seq_len, self._vocab.rel_size, seq_len * batch_size))
        # (#head x rel_size) x (#dep x batch_size)

        _target_vec = nd.array(targets_1D if is_train else flatten_numpy(arc_preds)).reshape(seq_len * batch_size, 1)
        _target_mat = _target_vec * nd.ones((1, self._vocab.rel_size))

        partial_rel_logits = nd.pick(flat_rel_logits, _target_mat.T, axis=0)
        # (rel_size) x (#dep x batch_size)

        if is_train or arc_targets is not None:
            rel_preds = partial_rel_logits.argmax(0)
            targets_1D = flatten_numpy(rel_targets)
            rel_correct = np.equal(rel_preds.asnumpy(), targets_1D).astype(np.float32) * mask_1D
            rel_accuracy = np.sum(rel_correct) / num_tokens
            losses = self.softmax_loss(partial_rel_logits, nd.array(targets_1D))
            rel_loss = nd.sum(losses * mask_1D_tensor) / num_tokens

        if not is_train:
            rel_probs = np.transpose(np.reshape(nd.softmax(nd.transpose(flat_rel_logits)).asnumpy(),
                                                (self._vocab.rel_size, seq_len, seq_len, batch_size), 'F'))
        # batch_size x #dep x #head x #nclasses

        if is_train or arc_targets is not None:
            loss = arc_loss + rel_loss
            correct = rel_correct * flatten_numpy(arc_correct)
            overall_accuracy = np.sum(correct) / num_tokens

        if is_train:
            return arc_accuracy, rel_accuracy, overall_accuracy, loss

        outputs = []

        for msk, arc_prob, rel_prob in zip(np.transpose(mask), arc_probs, rel_probs):
            # parse sentences one by one
            msk[0] = 1.
            sent_len = int(np.sum(msk))
            arc_pred = arc_argmax(arc_prob, sent_len, msk)
            rel_prob = rel_prob[np.arange(len(arc_pred)), arc_pred]
            rel_pred = rel_argmax(rel_prob, sent_len)
            outputs.append((arc_pred[1:sent_len], rel_pred[1:sent_len]))

        if arc_targets is not None:
            return arc_accuracy, rel_accuracy, overall_accuracy, outputs
        return outputs

    def save(self, save_path):
        self.save_parameters(save_path)

    def load(self, load_path):
        self.load_parameters(load_path)
