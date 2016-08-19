#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Trains, evaluates and saves the model network using a queue."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import scipy as scp
import random

from utils import train_utils

import tensorflow as tf


def _rezoom(hyp, pred_boxes, early_feat, early_feat_channels,
            w_offsets, h_offsets):
    '''
    Rezoom into a feature map at multiple interpolation points
    in a grid.

    If the predicted object center is at X, len(w_offsets) == 3,
    and len(h_offsets) == 5,
    the rezoom grid will look as follows:

    [o o o]
    [o o o]
    [o X o]
    [o o o]
    [o o o]

    Where each letter indexes into the feature map with bilinear interpolation
    '''

    grid_size = hyp['grid_width'] * hyp['grid_height']
    outer_size = grid_size * hyp['batch_size']
    indices = []
    for w_offset in w_offsets:
        for h_offset in h_offsets:
            indices.append(train_utils.bilinear_select(hyp,
                                                       pred_boxes,
                                                       early_feat,
                                                       early_feat_channels,
                                                       w_offset, h_offset))

    interp_indices = tf.concat(0, indices)
    rezoom_features = train_utils.interp(early_feat,
                                         interp_indices,
                                         early_feat_channels)
    rezoom_features_r = tf.reshape(rezoom_features,
                                   [len(w_offsets) * len(h_offsets),
                                    outer_size,
                                    hyp['rnn_len'],
                                    early_feat_channels])
    rezoom_features_t = tf.transpose(rezoom_features_r, [1, 2, 0, 3])
    return tf.reshape(rezoom_features_t,
                      [outer_size,
                       hyp['rnn_len'],
                       len(w_offsets) * len(h_offsets) * early_feat_channels])


def apply_rezoom(hyp, phase, early_feat, raw_output, pred_box,
                 pred_confidences):

    early_feat_channels = hyp['early_feat_channels']
    early_feat = early_feat[:, :, :, :early_feat_channels]
    grid_size = hyp['grid_width'] * hyp['grid_height']
    outer_size = grid_size * hyp['batch_size']

    pred_confs_deltas = []
    pred_boxes_deltas = []
    w_offsets = hyp['rezoom_w_coords']
    h_offsets = hyp['rezoom_h_coords']
    num_offsets = len(w_offsets) * len(h_offsets)
    rezoom_features = _rezoom(
        hyp, pred_box, early_feat, early_feat_channels,
        w_offsets, h_offsets)
    if phase == 'train':
        rezoom_features = tf.nn.dropout(rezoom_features, 0.5)
    for k in range(hyp['rnn_len']):
        delta_features = tf.concat(
            1, [raw_output, rezoom_features[:, k, :] / 1000.])
        dim = 128
        shape = [hyp['lstm_size'] + early_feat_channels * num_offsets,
                 dim]
        delta_weights1 = tf.get_variable('delta_ip1%d' % k,
                                         shape=shape)
        # TODO: add dropout here ?
        ip1 = tf.nn.relu(tf.matmul(delta_features, delta_weights1))
        if phase == 'train':
            ip1 = tf.nn.dropout(ip1, 0.5)
        delta_confs_weights = tf.get_variable(
            'delta_ip2%d' % k,
            shape=[dim, hyp['num_classes']])
        if hyp['reregress']:
            delta_boxes_weights = tf.get_variable(
                'delta_ip_boxes%d' % k,
                shape=[dim, 4])
            rere_feature = tf.matmul(ip1, delta_boxes_weights) * 5
            pred_boxes_deltas.append(tf.reshape(rere_feature,
                                                [outer_size, 1, 4]))
        scale = hyp.get('rezoom_conf_scale', 50)
        feature2 = tf.matmul(ip1, delta_confs_weights) * scale
        pred_confs_deltas.append(tf.reshape(feature2,
                                            [outer_size, 1,
                                             hyp['num_classes']]))
    pred_confs_deltas = tf.concat(1, pred_confs_deltas)

    # moved from loss
    pred_confs_deltas = tf.reshape(pred_confs_deltas,
                                   [outer_size * hyp['rnn_len'],
                                    hyp['num_classes']])

    pred_logits_squash = tf.reshape(pred_confs_deltas,
                                    [outer_size * hyp['rnn_len'],
                                     hyp['num_classes']])
    pred_confidences_squash = tf.nn.softmax(pred_logits_squash)
    pred_confidences = tf.reshape(pred_confidences_squash,
                                  [outer_size, hyp['rnn_len'],
                                   hyp['num_classes']])
    if hyp['reregress']:
        pred_boxes_deltas = tf.concat(1, pred_boxes_deltas)

    return pred_confs_deltas, pred_boxes_deltas


def _build_yolo_fc_layer(hyp, cnn_output):
    '''
    build simple overfeat decoder
    '''
    scale_down = 0.01
    grid_size = hyp['grid_width'] * hyp['grid_height']
    channels = hyp['cnn_channels']
    lstm_input = tf.reshape(cnn_output * scale_down,
                            (hyp['batch_size'] * grid_size, channels))

    initializer = tf.random_uniform_initializer(-0.1, 0.1)
    with tf.variable_scope('Overfeat', initializer=initializer):
        w = tf.get_variable('ip', shape=[hyp['cnn_channels'],
                                         hyp['lstm_size']])
        return tf.matmul(lstm_input, w)


def decoder(hyp, logits, phase):
    """Apply decoder to the logits.

    Computation which decode CNN boxes.
    The output can be interpreted as bounding Boxes.


    Args:
      logits: Logits tensor, output von encoder

    Return:
      decoded_logits: values which can be interpreted as bounding boxes
    """
    cnn_output, early_feat, _ = logits

    grid_size = hyp['grid_width'] * hyp['grid_height']
    outer_size = grid_size * hyp['batch_size']
    reuse = {'train': None, 'val': True}[phase]

    num_ex = hyp['batch_size'] * hyp['grid_width'] * hyp['grid_height']

    channels = hyp['cnn_channels']
    cnn_output = tf.reshape(cnn_output, [num_ex, channels])
    initializer = tf.random_uniform_initializer(-0.1, 0.1)
    with tf.variable_scope('decoder', reuse=reuse, initializer=initializer):

        raw_output = _build_yolo_fc_layer(hyp, cnn_output)

        if phase == 'train':
            raw_output = tf.nn.dropout(raw_output, 0.5)

        box_weights = tf.get_variable('box_ip',
                                      shape=(hyp['lstm_size'], 4))
        conf_weights = tf.get_variable('conf_ip',
                                       shape=(hyp['lstm_size'],
                                              hyp['num_classes']))

        pred_box = tf.reshape(tf.matmul(raw_output, box_weights) * 50,
                              [outer_size, 1, 4])

        pred_logits = tf.reshape(tf.matmul(raw_output, conf_weights),
                                 [outer_size, hyp['num_classes']])

        pred_confidences = tf.nn.softmax(pred_logits)

        pred_confidences = tf.reshape(pred_confidences,
                                      [outer_size, hyp['rnn_len'],
                                       hyp['num_classes']])

        if hyp['use_rezoom']:
            rezoom_deltas = apply_rezoom(hyp, phase, early_feat,
                                         raw_output, pred_box,
                                         pred_confidences)
        else:
            rezoom_deltas = (None, None)

    pred_confs_deltas, pred_boxes_deltas = rezoom_deltas

    return pred_box, pred_logits, pred_confidences,\
        pred_confs_deltas, pred_boxes_deltas


def loss(hypes, decoded_logits, labels, phase):
    """Calculate the loss from the logits and the labels.

    Args:
      decoded_logits: output of decoder
      labels: Labels tensor; Output from data_input

    Returns:
      loss: Loss tensor of type float.
    """

    flags, confidences, boxes = labels

    (pred_boxes, pred_logits, pred_confidences,
     pred_confs_deltas, pred_boxes_deltas) = decoded_logits

    grid_size = hypes['grid_width'] * hypes['grid_height']
    outer_size = grid_size * hypes['batch_size']

    with tf.variable_scope('decoder',
                           reuse={'train': None, 'val': True}[phase]):
        outer_boxes = tf.reshape(boxes, [outer_size, hypes['rnn_len'], 4])
        outer_flags = tf.cast(
            tf.reshape(flags, [outer_size, hypes['rnn_len']]), 'int32')
        if hypes['use_lstm']:
            assignments, classes, perm_truth, pred_mask = (
                tf.user_ops.hungarian(pred_boxes, outer_boxes, outer_flags,
                                      hypes['solver']['hungarian_iou']))
        else:
            classes = tf.reshape(flags, (outer_size, 1))
            perm_truth = tf.reshape(outer_boxes, (outer_size, 1, 4))
            pred_mask = tf.reshape(
                tf.cast(tf.greater(classes, 0), 'float32'), (outer_size, 1, 1))
        true_classes = tf.reshape(tf.cast(tf.greater(classes, 0), 'int64'),
                                  [outer_size * hypes['rnn_len']])
        pred_logit_r = tf.reshape(pred_logits,
                                  [outer_size * hypes['rnn_len'],
                                   hypes['num_classes']])

    grid_size = hypes['grid_width'] * hypes['grid_height']
    outer_size = grid_size * hypes['batch_size']

    cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
        pred_logit_r, true_classes)

    cross_entropy_sum = (tf.reduce_sum(cross_entropy))

    head = hypes['solver']['head_weights']
    confidences_loss = cross_entropy_sum / outer_size * head[0]
    residual = tf.reshape(perm_truth - pred_boxes * pred_mask,
                          [outer_size, hypes['rnn_len'], 4])

    boxes_loss = tf.reduce_sum(tf.abs(residual)) / outer_size * head[1]
    if hypes['use_rezoom']:
        if hypes['rezoom_change_loss'] == 'center':
            error = (perm_truth[:, :, 0:2] - pred_boxes[:, :, 0:2]) \
                / tf.maximum(perm_truth[:, :, 2:4], 1.)
            square_error = tf.reduce_sum(tf.square(error), 2)
            inside = tf.reshape(tf.to_int64(
                tf.logical_and(tf.less(square_error, 0.2**2),
                               tf.greater(classes, 0))), [-1])
        elif hypes['rezoom_change_loss'] == 'iou':
            pred_boxes_flat = tf.reshape(pred_boxes, [-1, 4])
            perm_truth_flat = tf.reshape(perm_truth, [-1, 4])
            iou = train_utils.iou(train_utils.to_x1y1x2y2(pred_boxes_flat),
                                  train_utils.to_x1y1x2y2(perm_truth_flat))
            inside = tf.reshape(tf.to_int64(tf.greater(iou, 0.5)), [-1])
        else:
            assert not hypes['rezoom_change_loss']
            inside = tf.reshape(tf.to_int64((tf.greater(classes, 0))), [-1])

        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
            pred_confs_deltas, inside)

        delta_confs_loss = tf.reduce_sum(cross_entropy) \
            / outer_size * hypes['solver']['head_weights'][0] * 0.1

        loss = confidences_loss + boxes_loss + delta_confs_loss

        if hypes['reregress']:
            delta_unshaped = perm_truth - (pred_boxes + pred_boxes_deltas)

            delta_residual = tf.reshape(delta_unshaped * pred_mask,
                                        [outer_size, hypes['rnn_len'], 4])
            sqrt_delta = tf.minimum(tf.square(delta_residual), 10. ** 2)
            delta_boxes_loss = (tf.reduce_sum(sqrt_delta) /
                                outer_size * head[1] * 0.03)
            boxes_loss = delta_boxes_loss

            tf.histogram_summary(
                phase + '/delta_hist0_x', pred_boxes_deltas[:, 0, 0])
            tf.histogram_summary(
                phase + '/delta_hist0_y', pred_boxes_deltas[:, 0, 1])
            tf.histogram_summary(
                phase + '/delta_hist0_w', pred_boxes_deltas[:, 0, 2])
            tf.histogram_summary(
                phase + '/delta_hist0_h', pred_boxes_deltas[:, 0, 3])
            loss += delta_boxes_loss
    else:
        loss = confidences_loss + boxes_loss

    tf.add_to_collection('losses', loss)

    total_loss = tf.add_n(tf.get_collection('losses'), name='total_loss')

    return total_loss, confidences_loss, boxes_loss


def evaluation(hyp, images, labels, decoded_logits, losses, global_step):

    loss, accuracy, confidences_loss, boxes_loss = {}, {}, {}, {}

    # Estimating Accuracy
    for phase in ['train', 'val']:

        flags, confidences, boxes = labels[phase]
        loss[phase], confidences_loss[phase], boxes_loss[phase] = losses[phase]

        (pred_boxes, pred_logits, pred_confidences,
         pred_confs_deltas, pred_boxes_deltas) = decoded_logits[phase]

        grid_size = hyp['grid_width'] * hyp['grid_height']

        pred_confidences_r = tf.reshape(
            pred_confidences,
            [hyp['batch_size'], grid_size, hyp['rnn_len'], hyp['num_classes']])
        pred_boxes_r = tf.reshape(
            pred_boxes, [hyp['batch_size'], grid_size, hyp['rnn_len'], 4])

        # Set up summary operations for tensorboard
        a = tf.equal(tf.argmax(confidences[:, :, 0, :], 2), tf.argmax(
            pred_confidences_r[:, :, 0, :], 2))
        accuracy[phase] = tf.reduce_mean(
            tf.cast(a, 'float32'), name=phase+'/accuracy')

    # Writing Metrics to Tensorboard

    moving_avg = tf.train.ExponentialMovingAverage(0.95)
    smooth_op = moving_avg.apply([accuracy['train'], accuracy['val'],
                                  confidences_loss[
                                      'train'], boxes_loss['train'],
                                  confidences_loss[
                                      'val'], boxes_loss['val'],
                                  ])

    for p in ['train', 'val']:
        tf.scalar_summary('%s/accuracy' % p, accuracy[p])
        tf.scalar_summary('%s/accuracy/smooth' %
                          p, moving_avg.average(accuracy[p]))
        tf.scalar_summary("%s/confidences_loss" % p, confidences_loss[p])
        tf.scalar_summary("%s/confidences_loss/smooth" % p,
                          moving_avg.average(confidences_loss[p]))
        tf.scalar_summary("%s/regression_loss" % p, boxes_loss[p])
        tf.scalar_summary("%s/regression_loss/smooth" % p,
                          moving_avg.average(boxes_loss[p]))

    test_image = images['val']
    # show ground truth to verify labels are correct
    test_true_confidences = confidences[0, :, :, :]
    test_true_boxes = boxes[0, :, :, :]

    # show predictions to visualize training progress
    test_pred_confidences = pred_confidences_r[0, :, :, :]
    test_pred_boxes = pred_boxes_r[0, :, :, :]

    def log_image(np_img, np_confidences, np_boxes, np_global_step,
                  pred_or_true):

        merged = train_utils.add_rectangles(hyp, np_img, np_confidences,
                                            np_boxes,
                                            use_stitching=True,
                                            rnn_len=hyp['rnn_len'])[0]

        num_images = 10

        filename = '%s_%s.jpg' % \
            ((np_global_step // hyp['logging']['display_iter'])
                % num_images, pred_or_true)
        img_path = os.path.join(hyp['dirs']['output_dir'], filename)

        scp.misc.imsave(img_path, merged)
        return merged

    pred_log_img = tf.py_func(log_image,
                              [test_image, test_pred_confidences,
                               test_pred_boxes, global_step, 'pred'],
                              [tf.float32])
    true_log_img = tf.py_func(log_image,
                              [test_image, test_true_confidences,
                               test_true_boxes, global_step, 'true'],
                              [tf.float32])
    tf.image_summary(phase + '/pred_boxes', tf.pack(pred_log_img),
                     max_images=10)
    tf.image_summary(phase + '/true_boxes', tf.pack(true_log_img),
                     max_images=10)

    return accuracy, smooth_op
