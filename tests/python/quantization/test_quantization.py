# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Some of the tests using CUDNN require a special GPU instruction called dp4a.
Ref: http://images.nvidia.com/content/pdf/tesla/184457-Tesla-P4-Datasheet-NV-Final-Letter-Web.pdf
"""
import os
import mxnet as mx
import numpy as np
from mxnet.gluon.model_zoo import vision
from mxnet.test_utils import assert_almost_equal, assert_exception, rand_ndarray, rand_shape_nd, same, DummyIter
from common import xfail_when_nonstandard_decimal_separator
from mxnet.io import NDArrayIter
import unittest
import operator


def initialize_block_params(block, initializer):
    for name, param in block.collect_params('.*gamma|.*running_var|.*moving_var').items():
        param.initialize(mx.init.Constant(1))
    for name, param in block.collect_params('.*beta|.*bias|.*moving_mean|.*running_mean').items():
        param.initialize(mx.init.Constant(0))
    for name, param in block.collect_params('.*weight').items():
        param.initialize(initializer)

def collect_block_args_aux(block, sym):
  arg_params, aux_params = dict(), dict()
  for k, v in block.collect_params().items():
    if k in sym.list_arguments():
      arg_params[k]= v._reduce()
    elif k in sym.list_auxiliary_states():
      aux_params[k]= v._reduce()
  return arg_params, aux_params

def is_test_for_gpu():
    return mx.current_context().device_type == 'gpu'


def is_test_for_mkldnn():
    return (mx.current_context().device_type == 'cpu'
            and os.environ.get('ENABLE_ONEDNN_QUANTIZATION_TEST') == '1')


def is_test_for_native_cpu():
    return (mx.current_context().device_type == 'cpu'
            and os.environ.get('ENABLE_ONEDNN_QUANTIZATION_TEST') == None)


def test_quantize_float32_to_int8():
    shape = rand_shape_nd(4)
    data = rand_ndarray(shape, 'default', dtype='float32')
    min_range = mx.nd.min(data)
    max_range = mx.nd.max(data)
    qdata, min_val, max_val = mx.nd.contrib.quantize(data, min_range, max_range, out_type='int8')
    data_np = data.asnumpy()
    min_range = min_range.asscalar()
    max_range = max_range.asscalar()
    real_range = np.maximum(np.abs(min_range), np.abs(max_range))
    quantized_range = 127.0
    scale = quantized_range / real_range
    assert qdata.dtype == np.int8
    assert min_val.dtype == np.float32
    assert max_val.dtype == np.float32
    assert same(min_val.asscalar(), -real_range)
    assert same(max_val.asscalar(), real_range)
    qdata_np = (np.sign(data_np) * np.minimum(np.abs(data_np) * scale + 0.5, quantized_range)).astype(np.int8)
    assert_almost_equal(qdata.asnumpy(), qdata_np, atol = 1)


def test_dequantize_int8_to_float32():

    def get_test_data(real_range, qdata_np):
        qdata = mx.nd.array(qdata_np, dtype=np.int8)
        min_range = mx.nd.array([-real_range], dtype=np.float32)
        max_range = mx.nd.array([real_range], dtype=np.float32)
        return qdata, min_range, max_range

    def baseline_dequantization(qdata, real_range, qdata_np):
        quantized_range = 127.0
        scale = real_range / quantized_range
        data_np = qdata_np * scale
        return data_np

    def test_nd_array_dequantization(qdata, min_range, max_range, expected_result):
        data = mx.nd.contrib.dequantize(qdata, min_range, max_range, out_type='float32')
        assert data.dtype == np.float32
        assert_almost_equal(data.asnumpy(), expected_result, atol = 1)

    def test_symbolic_api_dequantization(qdata, min_range, max_range, expected_result):
        sym_data = mx.sym.Variable('data')
        sym_min_range = mx.sym.Variable('min_range')
        sym_max_range = mx.sym.Variable('max_range')
        dequant = mx.sym.contrib.dequantize(sym_data, sym_min_range,
                                            sym_max_range, out_type='float32')
        out = dequant._bind(ctx=mx.current_context(),
                           args={'data':qdata, 'min_range':min_range, 'max_range':max_range})
        data = out.forward()[0]
        assert data.dtype == np.float32
        assert_almost_equal(data.asnumpy(), expected_result, atol = 1)

    real_range = 128
    shape = rand_shape_nd(4)
    qdata_np = np.random.uniform(low=-127, high=127, size=shape).astype(dtype=np.int8)
    qdata, min_range, max_range = get_test_data(real_range, qdata_np)
    expected_result = baseline_dequantization(qdata, real_range, qdata_np)
    # test nd array implementation.
    test_nd_array_dequantization(qdata, min_range, max_range, expected_result)
    # test symbolic api implementaion.
    test_symbolic_api_dequantization(qdata, min_range, max_range, expected_result)


def test_requantize_int32_to_int8():
    def quantized_int32_to_float(qdata, min_range, max_range):
        assert qdata.dtype == 'int32'
        quantized_range = np.iinfo('int32').max
        real_range = np.maximum(np.abs(min_range), np.abs(max_range))
        scale = float(real_range) / float(quantized_range)
        return qdata.astype('float32') * scale

    def float_to_quantized_int8(data, min_range, max_range):
        assert data.dtype == 'float32'
        real_range = np.maximum(np.abs(min_range), np.abs(max_range))
        quantized_range = np.iinfo('int8').max
        scale = float(quantized_range) / float(real_range)
        return (np.sign(data) * np.minimum(np.abs(data) * scale + 0.5, quantized_range)).astype('int8')

    def requantize(qdata, min_data, max_data, real_range):
        data = quantized_int32_to_float(qdata, min_data, max_data)
        output = float_to_quantized_int8(data, -real_range, real_range)
        return output, -real_range, real_range

    def requantize_baseline(qdata, min_data, max_data, min_calib_range=None, max_calib_range=None):
        if min_calib_range is not None and max_calib_range is not None:
            real_range = np.maximum(np.abs(min_calib_range), np.abs(max_calib_range))
            return requantize(qdata, min_data, max_data, real_range)
        else:
            min_range = quantized_int32_to_float(np.min(qdata), min_data, max_data)
            max_range = quantized_int32_to_float(np.max(qdata), min_data, max_data)
            return requantize(qdata, min_data, max_data, np.maximum(np.abs(min_range), np.abs(max_range)))

    def check_requantize(shape, min_calib_range=None, max_calib_range=None):
        qdata = mx.nd.random.uniform(low=-1000.0, high=1000.0, shape=shape).astype('int32')
        min_range = mx.nd.array([-1010.0])
        max_range = mx.nd.array([1020.0])
        if min_calib_range is None or max_calib_range is None:
            qdata_int8, min_output, max_output = mx.nd.contrib.requantize(qdata, min_range, max_range)
        else:
            qdata_int8, min_output, max_output = mx.nd.contrib.requantize(qdata, min_range, max_range,
                                                                          min_calib_range=min_calib_range,
                                                                          max_calib_range=max_calib_range)

        qdata_int8_np, min_output_np, max_output_np = requantize_baseline(qdata.asnumpy(), min_range.asscalar(),
                                                                          max_range.asscalar(),
                                                                          min_calib_range=min_calib_range,
                                                                          max_calib_range=max_calib_range)
        assert_almost_equal(qdata_int8.asnumpy(), qdata_int8_np, atol = 1)
        assert_almost_equal(min_output.asnumpy(), np.array([min_output_np]))
        assert_almost_equal(max_output.asnumpy(), np.array([max_output_np]))

    def check_requantize_with_symbol(shape, min_calib_range=None, max_calib_range=None):
        qdata = mx.nd.random.uniform(low=-1000.0, high=1000.0, shape=shape).astype('int32')
        min_range = mx.nd.array([-1010.0])
        max_range = mx.nd.array([1020.0])
        sym_data = mx.sym.Variable('data')
        sym_min_range = mx.sym.Variable('min_range')
        sym_max_range = mx.sym.Variable('max_range')
        if min_calib_range is None or max_calib_range is None:
            requant = mx.sym.contrib.requantize(sym_data, sym_min_range, sym_max_range)
            out = requant._bind(ctx=mx.current_context(),
                               args={'data':qdata, 'min_range':min_range,
                               'max_range':max_range})
            qdata_int8, min_output, max_output = out.forward()
        else:
            requant = mx.sym.contrib.requantize(sym_data, sym_min_range, sym_max_range,
                                                min_calib_range=min_calib_range,
                                                max_calib_range=max_calib_range)
            out = requant._bind(ctx=mx.current_context(), args={'data':qdata, 'min_range':min_range,
                               'max_range':max_range})
            qdata_int8, min_output, max_output = out.forward()

        qdata_int8_np, min_output_np, max_output_np = requantize_baseline(qdata.asnumpy(), min_range.asscalar(),
                                                                          max_range.asscalar(),
                                                                          min_calib_range=min_calib_range,
                                                                          max_calib_range=max_calib_range)
        assert_almost_equal(qdata_int8.asnumpy(), qdata_int8_np, atol = 1)
        assert_almost_equal(min_output.asnumpy(), np.array([min_output_np]))
        assert_almost_equal(max_output.asnumpy(), np.array([max_output_np]))

    # test with symbol API.
    check_requantize_with_symbol((3, 4, 10, 10))
    check_requantize_with_symbol((32, 3, 23, 23))
    check_requantize_with_symbol((3, 4, 10, 10), min_calib_range=-1050.0, max_calib_range=1040.0)
    check_requantize_with_symbol((32, 3, 23, 23), min_calib_range=-134.349, max_calib_range=523.43)
    # Test with nd array API
    check_requantize((3, 4, 10, 10))
    check_requantize((32, 3, 23, 23))
    check_requantize((3, 4, 10, 10), min_calib_range=-1050.0, max_calib_range=1040.0)
    check_requantize((32, 3, 23, 23), min_calib_range=-134.349, max_calib_range=523.43)


def test_quantized_conv():
    def check_quantized_conv(data_shape, kernel, num_filter, pad, stride, dilate, no_bias, qdtype):
        if is_test_for_native_cpu():
            print('skipped testing quantized_conv for native cpu since it is not supported yet')
            return
        elif is_test_for_mkldnn():
            # (TODO)Xinyu: https://github.com/apache/incubator-mxnet/issues/16830
            print('skipped testing quantized_conv for mkldnn cpu since it is a flaky case')
            return
        elif qdtype == 'uint8' and is_test_for_gpu():
            print('skipped testing quantized_conv for gpu uint8 since it is not supported yet')
            return
        elif is_test_for_gpu() and len(data_shape) != 4:
            print('skipped testing quantized_conv for gpu 5d layout since it is not supported yet')
            return

        # run fp32 conv
        data = mx.sym.Variable(name='data', shape=data_shape, dtype='float32')
        conv = mx.sym.Convolution(data=data, kernel=kernel, num_filter=num_filter, pad=pad, stride=stride,
                                  dilate=dilate, no_bias=no_bias, cudnn_off=False, name='conv')
        arg_shapes, _, _ = conv.infer_shape(data=data_shape)
        arg_names = conv.list_arguments()
        conv_exe_fp32 = conv._simple_bind(ctx=mx.current_context(), grad_req='null')
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 127.0
        else:
            data_low = -127.0
            data_high = 127.0
        conv_exe_fp32.arg_dict[arg_names[0]][:] = mx.nd.random.uniform(low=data_low, high=data_high,
                                                                       shape=data_shape).astype('int32')
        conv_exe_fp32.arg_dict[arg_names[1]][:] = mx.nd.random.uniform(low=-127.0, high=127.0,
                                                                       shape=arg_shapes[1]).astype('int32')
        if not no_bias:
            conv_exe_fp32.arg_dict[arg_names[2]][:] = mx.nd.random.uniform(low=-127.0, high=127.0,
                                                                           shape=arg_shapes[2]).astype('int32')
        output = conv_exe_fp32.forward()[0]

        # run quantized conv
        qdata = mx.sym.Variable(name='qdata', shape=data_shape, dtype=qdtype)
        qweight = mx.sym.Variable(name='qweight', dtype='int8')
        min_data = mx.sym.Variable(name='min_data')
        max_data = mx.sym.Variable(name='max_data')
        min_weight = mx.sym.Variable(name='min_weight')
        max_weight = mx.sym.Variable(name='max_weight')
        quantized_conv = mx.sym.contrib.quantized_conv(data=qdata, weight=qweight, min_data=min_data,
                                                       max_data=max_data, min_weight=min_weight,
                                                       max_weight=max_weight, kernel=kernel,
                                                       num_filter=num_filter, pad=pad, stride=stride,
                                                       dilate=dilate, no_bias=no_bias)
        qarg_names = quantized_conv.list_arguments()
        type_dict = None
        if not no_bias:
            type_dict = {qarg_names[2]: 'int8'}
        conv_exe_int8 = quantized_conv._simple_bind(ctx=mx.current_context(), type_dict=type_dict, grad_req='null')
        conv_exe_int8.arg_dict[qarg_names[0]][:] = conv_exe_fp32.arg_dict[arg_names[0]].astype(qdtype)
        conv_exe_int8.arg_dict[qarg_names[1]][:] = conv_exe_fp32.arg_dict[arg_names[1]].astype('int8')
        quantized_range = 127.0
        if no_bias:
            conv_exe_int8.arg_dict[qarg_names[2]][:] = -quantized_range
            conv_exe_int8.arg_dict[qarg_names[3]][:] = quantized_range
            conv_exe_int8.arg_dict[qarg_names[4]][:] = -quantized_range
            conv_exe_int8.arg_dict[qarg_names[5]][:] = quantized_range
        else:
            conv_exe_int8.arg_dict[qarg_names[2]][:] = conv_exe_fp32.arg_dict[arg_names[2]].astype('int8')
            conv_exe_int8.arg_dict[qarg_names[3]][:] = -quantized_range
            conv_exe_int8.arg_dict[qarg_names[4]][:] = quantized_range
            conv_exe_int8.arg_dict[qarg_names[5]][:] = -quantized_range
            conv_exe_int8.arg_dict[qarg_names[6]][:] = quantized_range
            conv_exe_int8.arg_dict[qarg_names[7]][:] = -quantized_range
            conv_exe_int8.arg_dict[qarg_names[8]][:] = quantized_range
        qoutput, min_range, max_range = conv_exe_int8.forward()

        if no_bias:
            assert_almost_equal(output.asnumpy(), qoutput.asnumpy(), atol = 1)
        else:
            # with adding bias, accuracy loss should not be greater than one
            diff = mx.nd.abs(output - qoutput.astype(output.dtype))
            cond = mx.nd.lesser(2, diff).sum().asscalar()
            assert cond == 0

    for qdtype in ['int8', 'uint8']:
        check_quantized_conv((3, 4, 28, 28), (3, 3), 128, (1, 1), (1, 1), (1, 1), True, qdtype)
        check_quantized_conv((3, 4, 28, 28), (3, 3), 128, (1, 1), (1, 1), (1, 1), False, qdtype)
        check_quantized_conv((1, 3, 4, 28, 28), (1, 3, 3), 128, (1, 1, 1), (1, 1, 1), (1, 1, 1), False, qdtype)
        check_quantized_conv((1, 3, 4, 28, 28), (1, 3, 3), 128, (1, 1, 1), (1, 1, 1), (1, 1, 1), True, qdtype)
        check_quantized_conv((1, 3, 4, 28, 28), (1, 3, 3), 128, (1, 1, 1), (1, 1, 1), (2, 2, 2), False, qdtype)
        check_quantized_conv((1, 3, 4, 28, 28), (1, 3, 3), 128, (1, 1, 1), (1, 1, 1), (2, 2, 2), True, qdtype)


def test_quantized_elemwise_add():
    def check_quantized_elemwise_add(data_shape, qtype):
        if is_test_for_native_cpu():
            print('skipped testing quantized_elemwise_add for native cpu since it is not supported yet')
            return
        elif qtype != 'uint8' and qtype != 'int8':
            print('skipped testing quantized_elemwise_add for not supported data type')
            return
        elif is_test_for_gpu():
            print('skipped testing quantized_elemwise_add for gpu since it is not supported yet')
            return

        dataA = mx.sym.Variable(name='dataA', shape=data_shape, dtype='float32')
        dataB = mx.sym.Variable(name='dataB', shape=data_shape, dtype='float32')
        elemwise_add_fp32 = mx.sym.elemwise_add(dataA, dataB)
        arg_names = elemwise_add_fp32.list_arguments()
        elemwise_add_fp32_exe = elemwise_add_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        if qtype == 'uint8':
            data_low = 0.0
            data_high = 255.0
        else:
            data_low = -127.0
            data_high = 127.0

        dataA_val = mx.nd.random.uniform(low=data_low, high=data_high, shape=data_shape).astype('int32')
        dataB_val = mx.nd.random.uniform(low=data_low, high=data_high, shape=data_shape).astype('int32')
        elemwise_add_fp32_exe.arg_dict[arg_names[0]][:] = dataA_val

        elemwise_add_fp32_exe.arg_dict[arg_names[1]][:] = dataB_val

        output = elemwise_add_fp32_exe.forward()[0]
        qdataA = mx.sym.Variable(name='qdataA', shape=data_shape, dtype=qtype)
        qdataB = mx.sym.Variable(name='qdataB', shape=data_shape, dtype=qtype)
        min_dataA = mx.sym.Variable(name='min_dataA', dtype='float32')
        max_dataA = mx.sym.Variable(name='max_dataA', dtype='float32')
        min_dataB = mx.sym.Variable(name='min_dataB', dtype='float32')
        max_dataB = mx.sym.Variable(name='max_dataB', dtype='float32')
        quantized_elemwise_add = mx.sym.contrib.quantized_elemwise_add(qdataA, qdataB, min_dataA, max_dataA, min_dataB, max_dataB)
        elemwise_add_int8_exe = quantized_elemwise_add._simple_bind(ctx=mx.current_context(), grad_req='null')
        qarg_names = quantized_elemwise_add.list_arguments()
        elemwise_add_int8_exe.arg_dict[qarg_names[0]][:] = elemwise_add_fp32_exe.arg_dict[arg_names[0]].astype(qtype)
        elemwise_add_int8_exe.arg_dict[qarg_names[1]][:] = elemwise_add_fp32_exe.arg_dict[arg_names[1]].astype(qtype)
        quantized_range = 127.0
        elemwise_add_int8_exe.arg_dict[qarg_names[2]][:] = data_low
        elemwise_add_int8_exe.arg_dict[qarg_names[3]][:] = data_high
        elemwise_add_int8_exe.arg_dict[qarg_names[4]][:] = data_low
        elemwise_add_int8_exe.arg_dict[qarg_names[5]][:] = data_high
        qoutput, min_range, max_range = elemwise_add_int8_exe.forward()
        int8_rslt = qoutput.astype(output.dtype)*max_range/0x7fffffff
        diff = mx.nd.abs(output - int8_rslt)
        cond = mx.nd.lesser(2, diff).sum().asscalar()
        assert cond == 0

    for qtype in ['int8', 'uint8']:
        check_quantized_elemwise_add((4, 6), qtype)
        check_quantized_elemwise_add((13, 74, 52), qtype)
        check_quantized_elemwise_add((3, 4, 56, 56), qtype)
        check_quantized_elemwise_add((32, 56, 64, 11), qtype)


def test_quantized_elemwise_mul():
    def check_quantized_elemwise_mul(data_shape, qtype):
        if is_test_for_native_cpu():
            print('skipped testing quantized_elemwise_mul for native cpu since it is not supported yet')
            return
        elif qtype != 'int8':
            print('skipped testing quantized_elemwise_mul for not supported data type')
            return
        elif is_test_for_gpu():
            print('skipped testing quantized_elemwise_mul for gpu since it is not supported yet')
            return

        dataA = mx.sym.Variable(name='dataA', shape=data_shape, dtype='float32')
        dataB = mx.sym.Variable(name='dataB', shape=data_shape, dtype='float32')
        elemwise_mul_fp32 = mx.sym.elemwise_mul(dataA, dataB)
        arg_names = elemwise_mul_fp32.list_arguments()
        elemwise_mul_fp32_exe = elemwise_mul_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        if qtype == 'uint8':
            data_low = 0.0
            data_high = 255.0
        else:
            data_low = -127.0
            data_high = 127.0

        dataA_val = mx.nd.random.uniform(low=data_low, high=data_high, shape=data_shape).astype('int32')
        dataB_val = mx.nd.random.uniform(low=data_low, high=data_high, shape=data_shape).astype('int32')
        elemwise_mul_fp32_exe.arg_dict[arg_names[0]][:] = dataA_val

        elemwise_mul_fp32_exe.arg_dict[arg_names[1]][:] = dataB_val

        output = elemwise_mul_fp32_exe.forward()[0]

        qdataA = mx.sym.Variable(name='qdataA', shape=data_shape, dtype=qtype)
        qdataB = mx.sym.Variable(name='qdataB', shape=data_shape, dtype=qtype)
        min_dataA = mx.sym.Variable(name='min_dataA', dtype='float32')
        max_dataA = mx.sym.Variable(name='max_dataA', dtype='float32')
        min_dataB = mx.sym.Variable(name='min_dataB', dtype='float32')
        max_dataB = mx.sym.Variable(name='max_dataB', dtype='float32')
        quantized_elemwise_mul = mx.sym.contrib.quantized_elemwise_mul(qdataA, qdataB, min_dataA, max_dataA, min_dataB, max_dataB)
        elemwise_mul_int8_exe = quantized_elemwise_mul._simple_bind(ctx=mx.current_context(), grad_req='null')
        qarg_names = quantized_elemwise_mul.list_arguments()
        elemwise_mul_int8_exe.arg_dict[qarg_names[0]][:] = elemwise_mul_fp32_exe.arg_dict[arg_names[0]].astype(qtype)
        elemwise_mul_int8_exe.arg_dict[qarg_names[1]][:] = elemwise_mul_fp32_exe.arg_dict[arg_names[1]].astype(qtype)
        quantized_range = 127.0
        elemwise_mul_int8_exe.arg_dict[qarg_names[2]][:] = data_low
        elemwise_mul_int8_exe.arg_dict[qarg_names[3]][:] = data_high
        elemwise_mul_int8_exe.arg_dict[qarg_names[4]][:] = data_low
        elemwise_mul_int8_exe.arg_dict[qarg_names[5]][:] = data_high
        qoutput, min_range, max_range = elemwise_mul_int8_exe.forward()

        fp32_rslt = output.asnumpy()
        int8_rslt = qoutput.astype(output.dtype)
        assert_almost_equal(fp32_rslt, int8_rslt, atol = 1e-4)

    for qtype in ['int8', 'uint8']:
        check_quantized_elemwise_mul((4, 6), qtype)
        check_quantized_elemwise_mul((13, 74, 52), qtype)
        check_quantized_elemwise_mul((3, 4, 56, 56), qtype)
        check_quantized_elemwise_mul((32, 56, 64, 11), qtype)


def test_quantized_pooling():
    def check_quantized_pooling(data_shape, kernel, pool_type, pad, stride, global_pool, qdtype, convention='valid'):
        if is_test_for_native_cpu():
            print('skipped testing quantized_pooling for native cpu since it is not supported yet')
            return
        elif qdtype == 'uint8' and is_test_for_gpu():
            print('skipped testing quantized_pooling for gpu uint8 since it is not supported yet')
            return
        elif is_test_for_gpu() and len(data_shape) != 4:
            print('skipped testing quantized_pooling for gpu 5d layout since it is not supported yet')
            return

        data = mx.sym.Variable(name='data', shape=data_shape, dtype='float32')
        pooling_fp32 = mx.sym.Pooling(data=data, kernel=kernel, pad=pad, stride=stride,
                                      pool_type=pool_type, global_pool=global_pool, cudnn_off=False,
                                      pooling_convention=convention)
        arg_shapes, _, _ = pooling_fp32.infer_shape(data=data_shape)
        arg_names = pooling_fp32.list_arguments()
        pooling_fp32_exe = pooling_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 127.0
        else:
            data_low = -127.0
            data_high = 127.0
        pooling_fp32_exe.arg_dict[arg_names[0]][:] = mx.nd.random.uniform(low=data_low, high=data_high,
                                                                            shape=data_shape).astype('int32')
        output = pooling_fp32_exe.forward()[0]

        qdata = mx.sym.Variable(name='qdata', shape=data_shape, dtype=qdtype)
        min_data = mx.sym.Variable(name='min_data')
        max_data = mx.sym.Variable(name='max_data')
        quantized_pooling = mx.sym.contrib.quantized_pooling(data=qdata, min_data=min_data,
                                                             max_data=max_data, kernel=kernel,
                                                             pad=pad, stride=stride, pool_type=pool_type,
                                                             global_pool=global_pool,
                                                             pooling_convention=convention)
        pooling_int8_exe = quantized_pooling._simple_bind(ctx=mx.current_context(), grad_req='null')
        qarg_names = quantized_pooling.list_arguments()
        pooling_int8_exe.arg_dict[qarg_names[0]][:] = pooling_fp32_exe.arg_dict[arg_names[0]].astype(qdtype)
        quantized_range = 127.0
        pooling_int8_exe.arg_dict[qarg_names[1]][:] = -quantized_range
        pooling_int8_exe.arg_dict[qarg_names[2]][:] = quantized_range
        qoutput, min_range, max_range = pooling_int8_exe.forward()

        if pool_type == 'max':
            assert_almost_equal(output.asnumpy(), qoutput.asnumpy())
        elif pool_type == 'avg':  # for avg pooling, fp32 and int8 may be different due to rounding errors
            diff = mx.nd.abs(output - qoutput.astype(output.dtype))
            cond = mx.nd.lesser(2, diff).sum().asscalar()
            assert cond == 0

    for qdtype in ['int8', 'uint8']:
        check_quantized_pooling((3, 4, 56, 56), (3, 3), 'max', (0, 0), (2, 2), False, qdtype)
        check_quantized_pooling((3, 4, 56, 56), (3, 3), 'max', (0, 0), (2, 2), True, qdtype)
        check_quantized_pooling((3, 512, 7, 7), (7, 7), 'avg', (0, 0), (1, 1), False, qdtype)
        check_quantized_pooling((3, 512, 7, 7), (7, 7), 'avg', (0, 0), (1, 1), True, qdtype)
        check_quantized_pooling((3, 4, 3, 56, 56), (1, 3, 3), 'max', (0, 0, 0), (1, 2, 2), False, qdtype)
        check_quantized_pooling((3, 4, 3, 56, 56), (1, 3, 3), 'max', (0, 0, 0), (1, 2, 2), True, qdtype)
        check_quantized_pooling((3, 512, 3, 7, 7), (1, 7, 7), 'avg', (0, 0, 0), (1, 2, 2), False, qdtype)
        check_quantized_pooling((3, 512, 3, 7, 7), (1, 7, 7), 'avg', (0, 0, 0), (1, 2, 2), True, qdtype)

        check_quantized_pooling((3, 4, 56, 56), (3, 3), 'max', (0, 0), (2, 2), False, qdtype, 'full')
        check_quantized_pooling((3, 4, 56, 56), (3, 3), 'max', (0, 0), (2, 2), True, qdtype, 'full')
        check_quantized_pooling((3, 512, 7, 7), (7, 7), 'avg', (0, 0), (1, 1), False, qdtype, 'full')
        check_quantized_pooling((3, 512, 7, 7), (7, 7), 'avg', (0, 0), (1, 1), True, qdtype, 'full')
        check_quantized_pooling((3, 4, 3, 56, 56), (1, 3, 3), 'max', (0, 0, 0), (1, 2, 2), False, qdtype, 'full')
        check_quantized_pooling((3, 4, 3, 56, 56), (1, 3, 3), 'max', (0, 0, 0), (1, 2, 2), True, qdtype, 'full')
        check_quantized_pooling((3, 512, 3, 7, 7), (1, 7, 7), 'avg', (0, 0, 0), (1, 2, 2), False, qdtype, 'full')
        check_quantized_pooling((3, 512, 3, 7, 7), (1, 7, 7), 'avg', (0, 0, 0), (1, 2, 2), True, qdtype, 'full')


def test_quantized_fc():
    def check_quantized_fc(data_shape, num_hidden, no_bias, qdtype, flatten=True):
        if is_test_for_native_cpu():
            hasMKL = False
            for key in os.environ.keys():
                if operator.eq(key, "BUILD_TAG"):
                    if os.environ['BUILD_TAG'].find("MKL") != -1:
                        hasMKL = True
                    break
            if hasMKL == False:
                print('skipped testing quantized_fc on cpu since s8u8s32 is only supported by MKL BLAS library')
                return
        elif qdtype == 'uint8' and is_test_for_gpu():
            print('skipped testing quantized_fc for gpu uint8 since it is not supported yet')
            return

        def maxabs(a, b):
            return mx.nd.maximum(mx.nd.abs(a), mx.nd.abs(b))

        data = mx.sym.Variable(name='data', shape=data_shape, dtype='float32')
        fc_fp32 = mx.sym.FullyConnected(data=data, num_hidden=num_hidden, no_bias=no_bias, flatten=flatten)
        arg_shapes, _, _ = fc_fp32.infer_shape(data=data_shape)
        arg_names = fc_fp32.list_arguments()
        fc_fp32_exe = fc_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        int8_range = 127.0
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 63.0
            quantized_range = 255.0
        else:
            data_low = -63.0
            data_high = 63.0
            quantized_range = 127.0

        data = mx.nd.random.uniform(low=data_low, high=data_high,
                                    shape=data_shape).astype('int32')
        weight = mx.nd.random.uniform(low=data_low, high=data_high,
                                      shape=arg_shapes[1]).astype('int32')
        fc_fp32_exe.arg_dict[arg_names[0]][:] = data
        fc_fp32_exe.arg_dict[arg_names[1]][:] = weight

        data_min = mx.nd.min(data).astype('float32')
        data_max = mx.nd.max(data).astype('float32')
        weight_min = mx.nd.min(weight).astype('float32')
        weight_max = mx.nd.max(weight).astype('float32')
        data_range = maxabs(data_min, data_max)
        weight_range = maxabs(weight_min, weight_max)

        if not no_bias:
            bias = mx.nd.random.uniform(low=data_low, high=data_high,
                                        shape=arg_shapes[2]).astype('int32')
            bias_min = mx.nd.min(bias).astype('float32')
            bias_max = mx.nd.max(bias).astype('float32')
            bias_range = maxabs(bias_min, bias_max)

            bias_scale = int8_range / bias_range
            data_scale = quantized_range / data_range
            weight_scale = int8_range / weight_range
            bias_int32_rescale = data_scale * weight_scale / bias_scale
            new_bias = mx.nd.cast(bias, dtype='float32') * bias_int32_rescale
            fc_fp32_exe.arg_dict[arg_names[2]][:] = new_bias.astype('int32')

        output = fc_fp32_exe.forward()[0]

        qdata = mx.sym.Variable(name='qdata', shape=data_shape, dtype=qdtype)
        fc_int8 = mx.sym.contrib.quantized_fully_connected(data=qdata, num_hidden=num_hidden,
                                                           no_bias=no_bias, flatten=flatten)
        qarg_names = fc_int8.list_arguments()
        type_dict = {qarg_names[1]: 'int8'}
        if not no_bias:
            type_dict.update({qarg_names[2]: 'int8'})
        fc_int8_exe = fc_int8._simple_bind(ctx=mx.current_context(), type_dict=type_dict, grad_req='null')
        fc_int8_exe.arg_dict[qarg_names[0]][:] = fc_fp32_exe.arg_dict[arg_names[0]].astype(qdtype)
        fc_int8_exe.arg_dict[qarg_names[1]][:] = fc_fp32_exe.arg_dict[arg_names[1]].astype('int8')
        if no_bias:
            fc_int8_exe.arg_dict[qarg_names[2]][:] = -data_range
            fc_int8_exe.arg_dict[qarg_names[3]][:] = data_range
            fc_int8_exe.arg_dict[qarg_names[4]][:] = -weight_range
            fc_int8_exe.arg_dict[qarg_names[5]][:] = weight_range
        else:
            fc_int8_exe.arg_dict[qarg_names[2]][:] = bias.astype('int8')
            fc_int8_exe.arg_dict[qarg_names[3]][:] = -data_range
            fc_int8_exe.arg_dict[qarg_names[4]][:] = data_range
            fc_int8_exe.arg_dict[qarg_names[5]][:] = -weight_range
            fc_int8_exe.arg_dict[qarg_names[6]][:] = weight_range
            fc_int8_exe.arg_dict[qarg_names[7]][:] = -bias_range
            fc_int8_exe.arg_dict[qarg_names[8]][:] = bias_range
        qoutput, min_range, max_range = fc_int8_exe.forward()

        if no_bias:
            assert_almost_equal(output.asnumpy(), qoutput.asnumpy())
        else:
            # with adding bias, accuracy loss should not be greater than one
            diff = mx.nd.abs(output - qoutput.astype(output.dtype))
            cond = mx.nd.lesser(2, diff).sum().asscalar()
            assert cond == 0

    for qdtype in ['int8', 'uint8']:
        if is_test_for_mkldnn():
            check_quantized_fc((32, 512, 2), 100, True, qdtype, flatten=False)
            check_quantized_fc((32, 512, 2), 100, False, qdtype, flatten=False)
            check_quantized_fc((32, 512, 2, 2), 100, True, qdtype, flatten=False)
            check_quantized_fc((32, 512, 2, 2), 100, False, qdtype, flatten=False)
        check_quantized_fc((32, 512, 2, 2), 100, True, qdtype)
        check_quantized_fc((32, 111, 2, 2), 100, True, qdtype)
        check_quantized_fc((32, 512, 2, 2), 100, False, qdtype)
        check_quantized_fc((32, 111, 2, 2), 100, False, qdtype)
        check_quantized_fc((256, 2048, 2, 2), 800, False, qdtype)
        check_quantized_fc((256, 111, 2, 2), 800, False, qdtype)
        check_quantized_fc((256, 2048, 2, 2), 800, True, qdtype)
        check_quantized_fc((256, 111, 2, 2), 800, True, qdtype)


def test_quantized_embedding():
    def check_quantized_embedding(data_shape, input_dim, output_dim):
        if is_test_for_gpu():
            print('skipped testing test_quantized_embedding for gpu since it is not supported yet')
            return

        def maxabs(a, b):
            return mx.nd.maximum(mx.nd.abs(a), mx.nd.abs(b))

        data0 = mx.sym.Variable(name='data', shape=data_shape, dtype='int32')
        embedding_fp32 = mx.sym.Embedding(data=data0, input_dim=input_dim, output_dim=output_dim)
        arg_shapes, _, _ = embedding_fp32.infer_shape(data=data_shape)
        arg_names = embedding_fp32.list_arguments()
        embedding_fp32_exe = embedding_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        int8_range = 127.0
        data = mx.nd.random.uniform(low=0, high=input_dim,
                                      shape=arg_shapes[0]).astype('int32')
        weight = mx.nd.random.uniform(low=-int8_range, high=int8_range,
                                      shape=arg_shapes[1]).astype('int32')
        embedding_fp32_exe.arg_dict[arg_names[0]][:] = data
        embedding_fp32_exe.arg_dict[arg_names[1]][:] = weight

        weight_min = mx.nd.min(weight).astype('float32')
        weight_max = mx.nd.max(weight).astype('float32')
        weight_range = maxabs(weight_min, weight_max)

        output = embedding_fp32_exe.forward()[0]

        embedding_int8 = mx.sym.contrib.quantized_embedding(data=data0, input_dim=input_dim, output_dim=output_dim)
        qarg_names = embedding_int8.list_arguments()
        type_dict = {qarg_names[1]: 'int8'}
        embedding_int8_exe = embedding_int8._simple_bind(ctx=mx.current_context(), type_dict=type_dict, grad_req='null')
        embedding_int8_exe.arg_dict[qarg_names[0]][:] = embedding_fp32_exe.arg_dict[arg_names[0]]
        embedding_int8_exe.arg_dict[qarg_names[1]][:] = embedding_fp32_exe.arg_dict[arg_names[1]].astype('int8')
        embedding_int8_exe.arg_dict[qarg_names[2]][:] = -weight_range
        embedding_int8_exe.arg_dict[qarg_names[3]][:] = weight_range
        qoutput, min_range, max_range = embedding_int8_exe.forward()

        assert_almost_equal(output.asnumpy(), qoutput.asnumpy())

    check_quantized_embedding((1,), 1000, 256)
    check_quantized_embedding((1,), 1024, 512)
    check_quantized_embedding((32,), 1000, 256)
    check_quantized_embedding((32,), 1024, 512)


def test_quantized_flatten():
    def check_quantized_flatten(shape, qdtype):
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 127.0
        else:
            data_low = -127.0
            data_high = 127.0
        qdata = mx.nd.random.uniform(low=data_low, high=data_high, shape=shape).astype(qdtype)
        min_data = mx.nd.array([-1023.343], dtype='float32')
        max_data = mx.nd.array([2343.324275], dtype='float32')
        qoutput, min_output, max_output = mx.nd.contrib.quantized_flatten(qdata, min_data, max_data)
        assert qoutput.ndim == 2
        assert qoutput.shape[0] == qdata.shape[0]
        assert qoutput.shape[1] == np.prod(qdata.shape[1:])
        assert same(qdata.asnumpy().flatten(), qoutput.asnumpy().flatten())
        assert same(min_data.asnumpy(), min_output.asnumpy())
        assert same(max_data.asnumpy(), max_output.asnumpy())

    for qdtype in ['int8', 'uint8']:
        check_quantized_flatten((10,), qdtype)
        check_quantized_flatten((10, 15), qdtype)
        check_quantized_flatten((10, 15, 18), qdtype)
        check_quantized_flatten((3, 4, 23, 23), qdtype)


def test_quantized_act():
    def check_quantized_act(data_shape, qdtype):
        if is_test_for_native_cpu():
            print('skipped testing quantized_act for native cpu since it is not supported yet')
            return
        elif qdtype == 'int8' and is_test_for_mkldnn():
            print('skipped testing quantized_act for mkldnn cpu int8 since it is not supported yet')
            return
        elif is_test_for_gpu():
            print('skipped testing quantized_act for gpu since it is not supported yet')
            return
        data = mx.sym.Variable(name='data', shape=data_shape, dtype='float32')
        act_fp32 = mx.sym.Activation(data=data, act_type='relu', name='relu')
        arg_shapes, _, _ = act_fp32.infer_shape(data=data_shape)
        arg_names = act_fp32.list_arguments()
        act_fp32_exe = act_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 127.0
        else:
            data_low = -127.0
            data_high = 127.0

        act_fp32_exe.arg_dict[arg_names[0]][:] = mx.nd.random.uniform(low=data_low,
                                                high=data_high, shape=data_shape).astype(qdtype)
        output = act_fp32_exe.forward()[0]

        qdata = mx.sym.Variable(name='qdata', shape=data_shape, dtype=qdtype)
        min_data = mx.sym.Variable(name='min_data')
        max_data = mx.sym.Variable(name='max_data')
        quantized_act = mx.sym.contrib.quantized_act(data=qdata, min_data=min_data, max_data=max_data, act_type='relu')
        act_int8_exe = quantized_act._simple_bind(ctx=mx.current_context(), grad_req='null')
        qarg_names = quantized_act.list_arguments()

        act_int8_exe.arg_dict[qarg_names[0]][:] = act_fp32_exe.arg_dict[arg_names[0]].astype(qdtype)
        quantized_range_min = mx.nd.min(act_int8_exe.arg_dict[qarg_names[0]][:])
        quantized_range_max = mx.nd.max(act_int8_exe.arg_dict[qarg_names[0]][:])
        act_int8_exe.arg_dict[qarg_names[1]][:] = quantized_range_min.astype(qdtype)
        act_int8_exe.arg_dict[qarg_names[2]][:] = quantized_range_max.astype(qdtype)
        qoutput, min_range, max_range = act_int8_exe.forward()

        assert_almost_equal(output.asnumpy(), qoutput.asnumpy())
        assert_almost_equal(min_range.asscalar(), quantized_range_min.asscalar())
        assert_almost_equal(max_range.asscalar(), quantized_range_max.asscalar())

    for qdtype in ['int8', 'uint8']:
        check_quantized_act((10,), qdtype)
        check_quantized_act((10, 15), qdtype)
        check_quantized_act((10, 15, 18), qdtype)
        check_quantized_act((3, 4, 23, 23), qdtype)


def test_quantized_bn():
    def get_mean_var(data):
        mean = mx.ndarray.mean(data, axis=1, exclude=1)
        mean_broad = mx.ndarray.expand_dims(mean, axis=0)
        mean_broad = mx.ndarray.expand_dims(mean_broad, axis=2)
        mean_broad = mx.ndarray.expand_dims(mean_broad, axis=3)
        mean_broad = mx.ndarray.broadcast_like(mean_broad, data)
        var = mx.ndarray.multiply(data - mean_broad, data - mean_broad)
        var = mx.ndarray.mean(var, axis=1, exclude=1)
        return mean, var

    def check_quantized_bn(data_shape, qdtype):
        if is_test_for_native_cpu():
            print('skipped testing quantize_bn for native cpu since it is not supported yet')
            return
        elif is_test_for_gpu():
            print('skipped testing quantize_bn for gpu since it is not supported yet')
            return

        # qdtype = uint8
        if qdtype == 'uint8':
            data_low = 0.0
            data_high = 255.0
        else:
            data_low = -127.0
            data_high = 127.0

        # run fp32 bn
        data_sym = mx.sym.Variable(name='data', shape=data_shape, dtype='float32')
        bn_fp32 = mx.sym.BatchNorm(data=data_sym, name='bn', use_global_stats=True, fix_gamma=False)
        arg_shapes, out_shapes, aux_shapes = bn_fp32.infer_shape(data=data_shape)
        arg_names = bn_fp32.list_arguments()
        aux_names = bn_fp32.list_auxiliary_states()
        data = mx.nd.random.uniform(low=data_low, high=data_high, shape=data_shape)
        gamma = mx.nd.random.uniform(low=data_low, high=data_high, shape=arg_shapes[1])
        beta = mx.nd.random.uniform(low=data_low, high=data_high, shape=arg_shapes[2])
        moving_mean, moving_var = get_mean_var(data)

        bn_fp32_exe = bn_fp32._simple_bind(ctx=mx.current_context(), grad_req='null')
        bn_fp32_exe.arg_dict[arg_names[0]][:] = data
        bn_fp32_exe.arg_dict[arg_names[1]][:] = gamma
        bn_fp32_exe.arg_dict[arg_names[2]][:] = beta
        bn_fp32_exe.aux_dict[aux_names[0]][:] = moving_mean
        bn_fp32_exe.aux_dict[aux_names[1]][:] = moving_var

        output = bn_fp32_exe.forward()[0]

        # generate int8 bn from fp32 bn
        arg_params = dict()
        for k, v in bn_fp32_exe.arg_dict.items():
            if 'data' in k or 'softmax_label' in k:
                continue
            arg_params[k] = v

        calib_data = mx.gluon.data.DataLoader(data, batch_size=data_shape[0])
        qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=bn_fp32,
                                                                         arg_params=arg_params,
                                                                         aux_params=bn_fp32_exe.aux_dict,
                                                                         ctx=mx.current_context(),
                                                                         quantized_dtype=qdtype,
                                                                         quantize_mode='full',
                                                                         calib_mode='naive',
                                                                         calib_data=calib_data,
                                                                         num_calib_batches=1)

        sym_block = mx.gluon.SymbolBlock(outputs=qsym, inputs=data_sym)
        params = qarg_params
        params.update(qaux_params)
        sym_block.load_dict(params)
        output_int8_to_fp32 = sym_block.forward(data)

        assert_almost_equal(output.asnumpy(), output_int8_to_fp32.asnumpy(), rtol=1e-1, atol=8)

    for qdtype in ['int8', 'uint8']:
      check_quantized_bn((32, 512, 4, 4), qdtype)
      check_quantized_bn((32, 1024, 8, 8), qdtype)
      check_quantized_bn((32, 3, 224, 224), qdtype)


def test_quantize_params():
    if is_test_for_native_cpu():
        print('skipped testing quantized_params for native cpu since it is not supported yet')
        return

    data = mx.sym.Variable('data')
    conv = mx.sym.Convolution(data, kernel=(1, 1), num_filter=2048, name='conv')
    sym = mx.sym.BatchNorm(data=conv, eps=2e-05, fix_gamma=False, momentum=0.9, use_global_stats=False, name='bn')
    offline_params = [name for name in sym.list_arguments()
                      if not name.startswith('data') and not name.endswith('label')]
    params = {}
    for name in offline_params:
        params[name] = mx.nd.uniform(shape=(2, 2))
    qsym, _ = mx.contrib.quant._quantize_symbol(sym, ctx=mx.current_context(),
                                                offline_params=offline_params, quantize_mode='full')
    qparams = mx.contrib.quant._quantize_params(qsym, params, min_max_dict = {})
    param_names = params.keys()
    qparam_names = qparams.keys()
    for name in qparam_names:
        if name.startswith('bn'):
            assert name in param_names
        elif name.startswith('conv'):
            assert name not in param_names
            assert name.find('quantize') != -1


def get_fp32_sym():
    data = mx.sym.Variable('data')
    conv = mx.sym.Convolution(data, kernel=(1, 1), num_filter=16, name='conv')
    bn = mx.sym.BatchNorm(data=conv, eps=2e-05, fix_gamma=False, momentum=0.9, use_global_stats=False, name='bn')
    act = mx.sym.Activation(data=bn, act_type='relu', name='relu')
    pool = mx.sym.Pooling(act, kernel=(4, 4), pool_type='avg', name='pool')
    fc = mx.sym.FullyConnected(pool, num_hidden=10, flatten=True, name='fc')
    sym = mx.sym.softmax(fc, name='softmax')
    return sym

def get_fp32_residual():
    data = mx.sym.Variable('data')
    conv0 = mx.sym.Convolution(data=data, num_filter=4, kernel=(1,1), pad=(0,0),
                               no_bias=True, name='conv0')
    bn = mx.sym.BatchNorm(data=conv0, fix_gamma=False, eps=2e-5, momentum=0.9, name='bn')
    sum0 = mx.sym.elemwise_add(bn, data, name='sum0')
    act0 = mx.sym.Activation(data=sum0, act_type='relu', name='relu0')
    pool0 = mx.sym.Pooling(act0, kernel=(4, 4), pool_type='avg', name='pool0')
    conv1 = mx.sym.Convolution(data=pool0, num_filter=4, kernel=(1,1), pad=(0,0),
                               no_bias=False, name='conv1')
    act1 = mx.sym.Activation(data=conv1, act_type='relu', name='relu1')
    pool1 = mx.sym.Pooling(act1, kernel=(4, 4), pool_type='avg', name='pool1')
    fc = mx.sym.FullyConnected(pool1, num_hidden=10, flatten=True, name='fc')
    sym = mx.sym.SoftmaxOutput(fc, grad_scale=1, ignore_label=-1, multi_output=False,
                               out_grad=False, preserve_shape=False, use_ignore=False, name='softmax')
    return sym

def get_fp32_sym_with_multiple_outputs(length=1):
    data = mx.sym.Variable('data')
    inputs = list(mx.sym.split(data, axis=0, num_outputs=length, squeeze_axis=1, name='split'))

    _conv_outs = []
    for i in range(length):
        _conv_outs.append(mx.sym.Convolution(data=inputs[i], kernel=(1, 1), num_filter=16, name='conv_{0}'.format(i)))
    conv_out = [mx.sym.expand_dims(i, axis=0) for i in _conv_outs]
    conv_out = mx.sym.Concat(*conv_out, dim=0, name='concat')
    reshape_out = mx.sym.reshape(data=conv_out, shape=((length, -1)), name='reshape')
    fc_out = mx.sym.FullyConnected(reshape_out, num_hidden=10, flatten=True, name='fc')
    sym = mx.sym.softmax(fc_out, name='softmax')
    return sym

def get_fp32_sym_with_multiple_inputs():
    data1 = mx.symbol.Variable('data1', shape=(64, 4, 10, 10), dtype='float32')
    data2 = mx.symbol.Variable('data2', shape=(64, 4, 10, 10), dtype='float32')
    weight1 = mx.symbol.Variable('conv1_weight', dtype='float32')
    weight2 = mx.symbol.Variable('conv2_weight', dtype='float32')
    conv1 = mx.symbol.Convolution(data=data1, weight=weight1, name='conv1', num_filter=64,
                            kernel=(1, 1), stride=(1, 1), no_bias=True)
    bn1 = mx.symbol.BatchNorm(data=conv1, name="bn1")
    conv2 = mx.symbol.Convolution(data=data2, weight=weight2, name='conv2', num_filter=64,
                            kernel=(1, 1), stride=(1, 1), no_bias=True)
    bn2 = mx.symbol.BatchNorm(data=conv2, name="bn2")
    sum = bn2 + bn1
    return sum


@xfail_when_nonstandard_decimal_separator
def test_quantize_model():
    def check_params(params, qparams, qsym=None):
        if qsym is None:
            assert len(params) == len(qparams)
            for k, v in params.items():
                assert k in qparams
                assert same(v.asnumpy(), qparams[k].asnumpy())
        else:
            qparams_ground_truth = mx.contrib.quant._quantize_params(qsym, params, min_max_dict = {})
            assert len(qparams) == len(qparams_ground_truth)
            for k, v in qparams_ground_truth.items():
                assert k in qparams
                assert same(v.asnumpy(), qparams[k].asnumpy())

    def check_qsym_calibrated(qsym):
        attrs = qsym.attr_dict()
        for k, v in attrs.items():
            if k.find('requantize_') != -1:
                assert 'min_calib_range' in v
                assert 'max_calib_range' in v

    def check_qsym_qdtype(qsym, qdtype):
        attrs = qsym.attr_dict()
        for k, v in attrs.items():
            if k.find('_quantize') != -1:
                assert 'out_type' in v
                assert v['out_type'] == qdtype

    def skip_not_supported():
        if is_test_for_native_cpu():
            print('skipped testing quantize_model for native cpu since it is not supported yet')
            return True
        elif qdtype == 'int8' and is_test_for_mkldnn():
            print('skipped testing quantize_model for mkldnn cpu int8 since it is not supported yet')
            return True
        elif qdtype == 'uint8' and is_test_for_gpu():
            print('skipped testing quantize_model for gpu uint8 since it is not supported yet')
            return True
        return False

    def check_quantize_model(qdtype):
        if is_test_for_native_cpu():
            print('skipped testing quantize_model for native cpu since it is not supported yet')
            return
        elif qdtype == 'int8' and is_test_for_mkldnn():
            print('skipped testing quantize_model for mkldnn cpu int8 since it is not supported yet')
            return
        elif qdtype == 'uint8' and is_test_for_gpu():
            print('skipped testing quantize_model for gpu uint8 since it is not supported yet')
            return

        sym = get_fp32_sym()
        batch_size = 4
        data_shape = (batch_size, 4, 10, 10)

        length = batch_size  # specify num of outputs from split op
        msym = get_fp32_sym_with_multiple_outputs(length)
        msym_data_shape = (length, 4, 4, 10, 10)

        for s, dshape in zip((sym, msym), (data_shape, msym_data_shape)):
            data = mx.sym.Variable('data')
            sym_block = mx.gluon.SymbolBlock(outputs=s, inputs=data)
            initialize_block_params(sym_block, mx.init.One())
            data = mx.nd.random.uniform(low=0, high=1, shape=dshape)
            sym_block.forward(data)
            arg_params, aux_params = collect_block_args_aux(sym_block, s)

            qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=s,
                                                                             arg_params=arg_params,
                                                                             aux_params=aux_params,
                                                                             ctx=mx.current_context(),
                                                                             quantized_dtype=qdtype,
                                                                             calib_mode='none',
                                                                             quantize_mode='full')
            check_params(arg_params, qarg_params, qsym)
            check_params(aux_params, qaux_params)

            calib_data = mx.nd.random.uniform(shape=dshape)
            calib_data = mx.gluon.data.DataLoader(calib_data, batch_size=batch_size)
            qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=s,
                                                                             arg_params=arg_params,
                                                                             aux_params=aux_params,
                                                                             ctx=mx.current_context(),
                                                                             quantized_dtype=qdtype,
                                                                             calib_mode='naive',
                                                                             calib_data=calib_data,
                                                                             num_calib_batches=1,
                                                                             quantize_mode='full')
            check_params(arg_params, qarg_params, qsym)
            check_params(aux_params, qaux_params)
            check_qsym_calibrated(qsym)
            check_qsym_qdtype(qsym, qdtype)

    def check_quantize_model_multiple_inputs(qdtype):
        if skip_not_supported():
            return

        sym = get_fp32_sym_with_multiple_inputs()
        dshape = (64, 4, 10, 10)
        data1 = mx.sym.Variable('data1')
        data2 = mx.sym.Variable('data2')
        sym_block = mx.gluon.SymbolBlock(outputs=sym, inputs=[data1, data2])
        initialize_block_params(sym_block, mx.init.One())
        data = [mx.nd.random.uniform(low=0, high=1, shape=dshape),
                mx.nd.random.uniform(low=0, high=1, shape=dshape)]
        sym_block.forward(*data)
        arg_params, aux_params = collect_block_args_aux(sym_block, sym)

        qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=sym,
                                                                            arg_params=arg_params,
                                                                            aux_params=aux_params,
                                                                            ctx=mx.current_context(),
                                                                            quantized_dtype=qdtype,
                                                                            calib_mode='none',
                                                                            quantize_mode='full')
        check_params(arg_params, qarg_params, qsym)
        check_params(aux_params, qaux_params)

        calib_data = [mx.nd.random.uniform(shape=dshape),
                      mx.nd.random.uniform(shape=dshape)]
        calib_data = mx.gluon.data.DataLoader(mx.gluon.data.ArrayDataset(*calib_data), batch_size=4)
        qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=sym,
                                                                         arg_params=arg_params,
                                                                         aux_params=aux_params,
                                                                         ctx=mx.current_context(),
                                                                         quantized_dtype=qdtype,
                                                                         calib_mode='naive',
                                                                         calib_data=calib_data,
                                                                         data_names=["data1","data2"],
                                                                         num_calib_batches=1,
                                                                         quantize_mode='full')
        check_params(arg_params, qarg_params, qsym)
        check_params(aux_params, qaux_params)
        check_qsym_calibrated(qsym)
        check_qsym_qdtype(qsym, qdtype)


    for qdtype in ['int8', 'uint8']:
        check_quantize_model(qdtype)
        check_quantize_model_multiple_inputs(qdtype)


def test_quantize_gluon_with_forward():
    def check_quantize_net(qdtype):
        if is_test_for_native_cpu():
            print('skipped testing test_quantize_model_with_forward for native cpu since it is not supported yet')
            return
        elif is_test_for_gpu():
            print('skipped testing test_quantize_model_with_forward for gpu uint8 since it is not supported yet')
            return

        data_shape = (32, 3, 224, 224)
        batch_size = 1
        resnet18_v1 = vision.resnet18_v1(pretrained=True)
        resnet18_v1.reset_ctx(mx.current_context())
        excluded_names_match = []
        if mx.current_context() == mx.gpu():
            excluded_names_match += ['activation', 'relu', 'conv0']
        num_calib_batches = 1

        random_data = mx.random.uniform(shape=data_shape)
        calib_data = mx.gluon.data.DataLoader(random_data, batch_size=batch_size)

        quantized_resnet18_v1 = mx.contrib.quant.quantize_net(resnet18_v1, quantized_dtype=qdtype,
                                                              exclude_layers=None,
                                                              exclude_layers_match=excluded_names_match,
                                                              calib_mode='none',
                                                              data_shapes=[data_shape],
                                                              ctx=mx.current_context())
        quantized_resnet18_v1.hybridize(static_alloc=True, static_shape=True)
        quantized_resnet18_v1(random_data)

        for mode in ['naive', 'entropy']:
            for quantize_granularity in ['tensor-wise', 'channel-wise']:
                qdtype = qdtype if mode is 'naive' else 'auto'
                quantized_resnet18_v1 = mx.contrib.quant.quantize_net(resnet18_v1, quantized_dtype=qdtype,
                                                                    exclude_layers=None,
                                                                    exclude_layers_match=excluded_names_match,
                                                                    calib_data=calib_data,
                                                                    calib_mode=mode,
                                                                    quantize_granularity=quantize_granularity,
                                                                    num_calib_batches=num_calib_batches,
                                                                    ctx=mx.current_context())

                quantized_resnet18_v1.hybridize(static_alloc=True, static_shape=True)
                quantized_resnet18_v1(random_data)

    for qdtype in ['int8', 'uint8']:
        check_quantize_net(qdtype)


@xfail_when_nonstandard_decimal_separator
def test_quantize_sym_with_calib():
    if is_test_for_native_cpu():
        print('skipped testing quantized_pooling for native cpu since it is not supported yet')
        return

    sym = get_fp32_sym()
    offline_params = [name for name in sym.list_arguments()
                      if not name.startswith('data') and not name.endswith('label')]
    qsym, _ = mx.contrib.quant._quantize_symbol(sym, ctx=mx.current_context(),
                                             offline_params=offline_params, quantize_mode='full')
    requantize_op_names = ['requantize_conv', 'requantize_fc']
    min_max_dict = {'conv_output': (np.random.uniform(low=100.0, high=200.0), np.random.uniform(low=100.0, high=200.0)),
                    'fc_output': (np.random.uniform(low=100.0, high=200.0), np.random.uniform(low=100.0, high=200.0))}
    op_name_to_th_name = {'requantize_conv': 'conv_output', 'requantize_fc': 'fc_output'}
    cqsym = mx.contrib.quant._calibrate_quantized_sym(qsym, min_max_dict)
    attr_dict = cqsym.attr_dict()
    for name in requantize_op_names:
        assert name in attr_dict
        lhs = float(attr_dict[name]['min_calib_range'])
        rhs = min_max_dict[op_name_to_th_name[name]][0]
        assert_almost_equal(np.array([lhs]), np.array([rhs]))
        lhs = float(attr_dict[name]['max_calib_range'])
        rhs = min_max_dict[op_name_to_th_name[name]][1]
        assert_almost_equal(np.array([lhs]), np.array([rhs]), rtol=1e-3, atol=1e-4)


def test_quantization_net_with_different_data_inputs_options():
    if is_test_for_native_cpu():
        print('skipped testing test_quantization_net_with_different_data_inputs_options for native cpu since it is not supported yet')
        return
    elif is_test_for_gpu():
        print('skipped testing test_quantization_net_with_different_data_inputs_options for gpu since it is not supported yet')
        return

    sym = get_fp32_sym()
    net = mx.gluon.SymbolBlock(sym, mx.sym.var('data'))
    initialize_block_params(net, mx.init.Normal(0.2))

    batch_size = 32
    data_shape = (batch_size, 3, 224, 224)
    random_data = mx.random.uniform(shape=data_shape)

    # pass data_shapes as list of tuples
    quantized_net = mx.contrib.quant.quantize_net(net,
                                                  quantized_dtype='auto',
                                                  data_shapes=[data_shape],
                                                  ctx=mx.current_context())
    out = quantized_net(random_data)
    out.wait_to_read()


    # pass data_shapes as list of DataDescs
    net2 = mx.gluon.SymbolBlock(sym, mx.sym.var('data'))
    initialize_block_params(net2, mx.init.Normal(0.2))
    data_desc = mx.io.DataDesc('data', data_shape)
    quantized_net2 = mx.contrib.quant.quantize_net(net,
                                                   quantized_dtype='auto',
                                                   data_shapes=[data_desc],
                                                   ctx=mx.current_context())
    out2 = quantized_net2(random_data)
    out2.wait_to_read()


    # pass data as DataLoader
    net3 = mx.gluon.SymbolBlock(sym, mx.sym.var('data'))
    initialize_block_params(net3, mx.init.Normal(0.2))
    data_loader = mx.gluon.data.DataLoader(random_data, batch_size=batch_size)
    quantized_net3 = mx.contrib.quant.quantize_net(net,
                                                   quantized_dtype='auto',
                                                   calib_data=data_loader,
                                                   ctx=mx.current_context())
    out3 = quantized_net3(random_data)
    out3.wait_to_read()



def test_optimal_threshold_adversarial_case():
    # The worst case for the optimal_threshold function is when the values are concentrated
    # at one edge: [0, 0, ..., 1000]. (histogram)
    # We want to make sure that the optimal threshold in this case is the max.
    hist = []
    hist_edges = []
    min_val = -2
    max_val = 2
    for i in range(0, 998):
        hist.append(0)
    for i in range(0, 999):
        hist_edges.append((max_val - min_val) / 999 * i + min_val)
    hist.append(1000)
    hist_edges.append(max_val)
    hist_data = (hist, hist_edges, min_val, max_val, max_val)
    for dtype in ['uint8', 'int8', 'auto']:
        res = mx.contrib.quant._LayerHistogramCollector.get_optimal_threshold(hist_data, dtype, num_quantized_bins=5)
        # The threshold should be 2.
        print (res)
        assert abs(res[2] - 2) < 1e-5


def test_get_optimal_thresholds():
    # Given an ndarray with elements following a uniform distribution, the optimal threshold
    # for quantizing the ndarray should be either abs(min(nd)) or abs(max(nd)).
    def get_threshold(nd):
        min_nd = mx.nd.min(nd)
        max_nd = mx.nd.max(nd)
        return mx.nd.maximum(mx.nd.abs(min_nd), mx.nd.abs(max_nd)).asnumpy()

    for dtype in ['uint8', 'int8', 'auto']:
        nd = mx.nd.uniform(low=-10.532, high=11.3432, shape=(8, 3, 23, 23), dtype=np.float64)
        expected_threshold = get_threshold(nd)
        arr = nd.asnumpy()
        min_range = np.min(arr)
        max_range = np.max(arr)
        th = max(abs(min_range), abs(max_range))
        hist, hist_edges = np.histogram(arr, bins=8001, range=(-th, th))
        hist_dict = {'layer1' : (hist, hist_edges, min_range, max_range, th)}
        min_max_dict = mx.contrib.quant._LayerHistogramCollector.get_optimal_thresholds(hist_dict, dtype)
        assert 'layer1' in min_max_dict
        assert_almost_equal(np.array([min_max_dict['layer1'][1]]), expected_threshold, rtol=1e-2, atol=1e-4)

