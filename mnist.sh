#!/bin/bash

export RANK=0
export WORLD_SIZE=1
export MASTER_ADDR=127.0.0.1
export MASTER_PORT=6006

python3 submission_runner.py \
    --framework=pytorch \
    --workload=mnist \
    --experiment_dir=/checkpoint/adefazio/mnist \
    --experiment_name=baseline \
    --submission_path=reference_algorithms/development_algorithms/mnist/mnist_pytorch/submission.py \
    --tuning_search_space=reference_algorithms/development_algorithms/mnist/tuning_search_space.json