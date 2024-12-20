#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ERNIE pretraining."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import multiprocessing

import numpy as np
import paddle.fluid as fluid

from reader.pretraining import ErnieDataReader
from model.ernie import ErnieModel, ErnieConfig
from optimization import optimization
from utils.args import print_arguments
from utils.init import init_checkpoint, init_pretraining_params

from pretrain_args import parser

args = parser.parse_args()

# yapf: enable.


def create_model(pyreader_name, ernie_config):
    pyreader = fluid.layers.py_reader(
        capacity=70,
        shapes=[[-1, args.max_seq_len, 1], [-1, args.max_seq_len, 1],
                [-1, args.max_seq_len, 1], [-1, args.max_seq_len, 1], [-1, 1],
                [-1, 1], [-1, 1]],
        dtypes=[
            'int64', 'int64', 'int64', 'float32', 'int64', 'int64', 'int64'
        ],
        lod_levels=[0, 0, 0, 0, 0, 0, 0],
        name=pyreader_name,
        use_double_buffer=True)

    (src_ids, pos_ids, sent_ids, input_mask, mask_label, mask_pos,
     labels) = fluid.layers.read_file(pyreader)

    ernie = ErnieModel(
        src_ids=src_ids,
        position_ids=pos_ids,
        sentence_ids=sent_ids,
        input_mask=input_mask,
        config=ernie_config,
        weight_sharing=args.weight_sharing,
        use_fp16=args.use_fp16)

    next_sent_acc, mask_lm_loss, total_loss = ernie.get_pretraining_output(
        mask_label, mask_pos, labels)

    return pyreader, next_sent_acc, mask_lm_loss, total_loss


def predict_wrapper(args,
                    exe,
                    ernie_config,
                    test_prog=None,
                    pyreader=None,
                    fetch_list=None):
    # Context to do validation.
    filelist = args.test_filelist if args.do_test else args.valid_filelist
    data_reader = ErnieDataReader(
        filelist,
        vocab_path=args.vocab_path,
        batch_size=args.batch_size,
        voc_size=ernie_config['vocab_size'],
        shuffle_files=False,
        epoch=1,
        max_seq_len=args.max_seq_len,
        hack_old_trainset=args.hack_old_data,
        is_test=True)

    if args.do_test:
        assert args.init_checkpoint is not None, "[FATAL] Please use --init_checkpoint '/path/to/checkpoints' \
                                                  to specify you pretrained model checkpoints"

        init_pretraining_params(exe, args.init_checkpoint, test_prog)

    def predict(exe=exe, pyreader=pyreader):

        pyreader.decorate_tensor_provider(data_reader.data_generator())
        pyreader.start()

        cost = 0
        lm_cost = 0
        acc = 0
        steps = 0
        time_begin = time.time()
        while True:
            try:
                each_next_acc, each_mask_lm_cost, each_total_cost = exe.run(
                    fetch_list=fetch_list, program=test_prog)
                acc += each_next_acc
                lm_cost += each_mask_lm_cost
                cost += each_total_cost
                steps += 1
                if args.do_test and steps % args.skip_steps == 0:
                    print("[test_set] steps: %d" % steps)

            except fluid.core.EOFException:
                pyreader.reset()
                break

        used_time = time.time() - time_begin
        return cost, lm_cost, acc, steps, (args.skip_steps / used_time)

    return predict


def test(args):
    ernie_config = ErnieConfig(args.ernie_config_path)
    ernie_config.print_config()

    test_prog = fluid.Program()
    test_startup = fluid.Program()
    with fluid.program_guard(test_prog, test_startup):
        with fluid.unique_name.guard():
            test_pyreader, next_sent_acc, mask_lm_loss, total_loss = create_model(
                pyreader_name='test_reader', ernie_config=ernie_config)

    test_prog = test_prog.clone(for_test=True)

    place = fluid.CUDAPlace(0) if args.use_cuda == True else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(test_startup)

    predict = predict_wrapper(
        args,
        exe,
        ernie_config,
        test_prog=test_prog,
        pyreader=test_pyreader,
        fetch_list=[next_sent_acc.name, mask_lm_loss.name, total_loss.name])

    print("test begin")
    loss, lm_loss, acc, steps, speed = predict()
    print(
        "[test_set] loss: %f, global ppl: %f, next_sent_acc: %f, speed: %f steps/s"
        % (np.mean(np.array(loss) / steps),
           np.exp(np.mean(np.array(lm_loss) / steps)),
           np.mean(np.array(acc) / steps), speed))


def train(args):
    ernie_config = ErnieConfig(args.ernie_config_path)
    ernie_config.print_config()
    print("pretraining start")
    
    node_nums = int(os.getenv("PADDLE_NODES_NUM"))
    train_program = fluid.Program()
    startup_prog = fluid.Program()
    with fluid.program_guard(train_program, startup_prog):
        with fluid.unique_name.guard():
            train_pyreader, next_sent_acc, mask_lm_loss, total_loss = create_model(
                pyreader_name='train_reader', ernie_config=ernie_config)
            scheduled_lr, loss_scaling = optimization(
                loss=total_loss,
                warmup_steps=args.warmup_steps,
                num_train_steps=args.num_train_steps // node_nums,
                learning_rate=args.learning_rate,
                train_program=train_program,
                startup_prog=startup_prog,
                weight_decay=args.weight_decay,
                scheduler=args.lr_scheduler,
                use_fp16=args.use_fp16,
                use_dynamic_loss_scaling=args.use_dynamic_loss_scaling,
                init_loss_scaling=args.init_loss_scaling,
                incr_every_n_steps=args.incr_every_n_steps,
                decr_every_n_nan_or_inf=args.decr_every_n_nan_or_inf,
                incr_ratio=args.incr_ratio,
                decr_ratio=args.decr_ratio)
            """
            fluid.memory_optimize(
                input_program=train_program,
                skip_opt_set=[
                    next_sent_acc.name, mask_lm_loss.name, total_loss.name
                ])
            """

    test_prog = fluid.Program()
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            test_pyreader, next_sent_acc, mask_lm_loss, total_loss = create_model(
                pyreader_name='test_reader', ernie_config=ernie_config)

    test_prog = test_prog.clone(for_test=True)

    gpu_id = 0 
    gpus = fluid.core.get_cuda_device_count()
    if args.is_distributed:
        gpus = os.getenv("FLAGS_selected_gpus").split(",")
        gpu_id = int(gpus[0])

    if args.use_cuda:
        place = fluid.CUDAPlace(gpu_id)
        dev_count = len(gpus) if args.is_distributed else gpus
    else:
        place = fluid.CPUPlace()
        dev_count = int(os.environ.get('CPU_NUM', multiprocessing.cpu_count()))

    print("Device count %d, gpu_id:%d" % (dev_count, gpu_id))
    print("theoretical memory usage: ")
    print(fluid.contrib.memory_usage(
        program=train_program, batch_size=args.batch_size // args.max_seq_len))

    nccl2_num_trainers = 1
    nccl2_trainer_id = 0
    print("args.is_distributed:", args.is_distributed)
    
    trainer_id = 0
    
    if args.is_distributed:
        trainer_id = int(os.getenv("PADDLE_TRAINER_ID"))
        worker_endpoints_env = os.getenv("PADDLE_TRAINER_ENDPOINTS")
        current_endpoint = os.getenv("PADDLE_CURRENT_ENDPOINT")
        worker_endpoints = worker_endpoints_env.split(",")
        trainers_num = len(worker_endpoints)

        print("worker_endpoints:{} trainers_num:{} current_endpoint:{} \
              trainer_id:{}".format(worker_endpoints, trainers_num,
                                    current_endpoint, trainer_id))

        # prepare nccl2 env.
        config = fluid.DistributeTranspilerConfig()
        config.mode = "nccl2"
        if args.nccl_comm_num > 1:
            config.nccl_comm_num = args.nccl_comm_num
        if args.use_hierarchical_allreduce and trainers_num > args.hierarchical_allreduce_inter_nranks:
            config.use_hierarchical_allreduce=args.use_hierarchical_allreduce
            config.hierarchical_allreduce_inter_nranks=args.hierarchical_allreduce_inter_nranks

            assert config.hierarchical_allreduce_inter_nranks > 1
            assert trainers_num % config.hierarchical_allreduce_inter_nranks == 0

            config.hierarchical_allreduce_exter_nranks = \
                trainers_num / config.hierarchical_allreduce_inter_nranks

        t = fluid.DistributeTranspiler(config=config)
        t.transpile(
            trainer_id,
            trainers=worker_endpoints_env,
            current_endpoint=current_endpoint,
            program=train_program,
            startup_program=startup_prog)
        nccl2_num_trainers = trainers_num
        nccl2_trainer_id = trainer_id

    exe = fluid.Executor(place)
    exe.run(startup_prog)

    if args.init_checkpoint and args.init_checkpoint != "":
        init_checkpoint(exe, args.init_checkpoint, train_program, args.use_fp16)

    data_reader = ErnieDataReader(
        filelist=args.train_filelist,
        batch_size=args.batch_size,
        vocab_path=args.vocab_path,
        voc_size=ernie_config['vocab_size'],
        random_seed=args.random_seed,
        epoch=args.epoch,
        max_seq_len=args.max_seq_len,
        generate_neg_sample=args.generate_neg_sample,
        hack_old_trainset=args.hack_old_data)

    exec_strategy = fluid.ExecutionStrategy()
    if args.use_fast_executor:
        exec_strategy.use_experimental_executor = True
    exec_strategy.num_threads = 2
    if args.use_fp16:
        exec_strategy.num_threads = 4
    exec_strategy.num_iteration_per_drop_scope = min(10, args.skip_steps)

    build_strategy = fluid.BuildStrategy()
    build_strategy.remove_unnecessary_lock = False
    if args.use_fuse:
        build_strategy.fuse_all_reduce_ops = True

    train_exe = fluid.ParallelExecutor(
        use_cuda=args.use_cuda,
        loss_name=total_loss.name,
        build_strategy=build_strategy,
        exec_strategy=exec_strategy,
        main_program=train_program,
        num_trainers=nccl2_num_trainers,
        trainer_id=nccl2_trainer_id)

    if args.valid_filelist and args.valid_filelist != "":
        predict = predict_wrapper(
            args,
            exe,
            ernie_config,
            test_prog=test_prog,
            pyreader=test_pyreader,
            fetch_list=[
                next_sent_acc.name, mask_lm_loss.name, total_loss.name
            ])

    train_pyreader.decorate_tensor_provider(data_reader.data_generator())
    train_pyreader.start()
    steps = 0
    cost = []
    lm_cost = []
    acc = []
    time_begin = time.time()
    while steps < args.num_train_steps:
        try:
            steps += node_nums
            skip_steps = args.skip_steps * node_nums
            
            fetch_list = []
            if nccl2_trainer_id == 0 and steps % skip_steps == 0:
                fetch_list = [next_sent_acc.name, mask_lm_loss.name, total_loss.name,
                        scheduled_lr.name]
                if args.use_fp16:
                    fetch_list.append(loss_scaling.name)

            ret = train_exe.run(fetch_list=fetch_list)
            time_end = time.time()
            used_time = time_end - time_begin
            
            if ret:
                each_next_acc, each_mask_lm_cost, each_total_cost, np_lr, l_scaling = ret \
                    if args.use_fp16 else ret + [[args.init_loss_scaling]]
                
                acc.extend(each_next_acc)
                lm_cost.extend(each_mask_lm_cost)
                cost.extend(each_total_cost)
                
                epoch, current_file_index, total_file, current_file, mask_type = data_reader.get_progress(
                )
                
                print("feed_queue size", train_pyreader.queue.size())
                print("current learning_rate:%f, loss scaling:%f" % (np_lr[0], l_scaling[0]))
                print(
                    "epoch: %d, progress: %d/%d, step: %d, loss: %f, "
                    "ppl: %f, next_sent_acc: %f, speed: %f steps/s, file: %s, mask_type: %s"
                    % (epoch, current_file_index, total_file, steps,
                       np.mean(np.array(cost)),
                       np.mean(np.exp(np.array(lm_cost))),
                       np.mean(np.array(acc)), skip_steps / used_time,
                       current_file, mask_type))
                cost = []
                lm_cost = []
                acc = []
                time_begin = time.time()
            elif steps % skip_steps == 0:
                epoch, current_file_index, total_file, current_file, mask_type = data_reader.get_progress(
                )
                print("feed_queue size", train_pyreader.queue.size())
                print("epoch: %d, progress: %d/%d, step: %d, "
                        "speed: %f steps/s, file: %s, mask_type: %s"
                        % (epoch, current_file_index, total_file, steps,
                            skip_steps / used_time, current_file, mask_type))
                time_begin = time.time()

            if not nccl2_trainer_id == 0:
                continue

            if steps % args.save_steps == 0:
                save_path = os.path.join(args.checkpoints, "step_" + str(steps))
                fluid.io.save_persistables(exe, save_path, train_program)

            if args.valid_filelist and steps % args.validation_steps == 0:
                vali_cost, vali_lm_cost, vali_acc, vali_steps, vali_speed = predict(
                )
                print("[validation_set] epoch: %d, step: %d, "
                      "loss: %f, global ppl: %f, batch-averged ppl: %f, "
                      "next_sent_acc: %f, speed: %f steps/s" %
                      (epoch, steps, np.mean(np.array(vali_cost) / vali_steps),
                       np.exp(np.mean(np.array(vali_lm_cost) / vali_steps)),
                       np.mean(np.exp(np.array(vali_lm_cost) / vali_steps)),
                       np.mean(np.array(vali_acc) / vali_steps), vali_speed))

        except fluid.core.EOFException:
            train_pyreader.reset()
            break


if __name__ == '__main__':
    print_arguments(args)
    if args.do_test:
        test(args)
    else:
        train(args)
