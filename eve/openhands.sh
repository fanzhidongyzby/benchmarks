# 清理
kill -9 `ps aux | grep -E '(infer|eval)' | grep -v grep | awk '{print $2}'`
docker ps -a | awk '{print $1}' | grep -v CONTAINER | xargs docker rm -f
rm -rf eval_outputs runid/* infer.log eval.log submit.log eve_eval_result.json
ps aux | grep -E '(infer|eval)'
docker ps -a

# 初始化
nohup bash sync.sh $HOST $PASSWD 8 merge &> s.log &

# 镜像：
uv run python -m benchmarks.swebench.build_images \
  --dataset princeton-nlp/SWE-bench_Verified \
  --split test \
  --image ghcr.io/openhands/eval-agent-server \
  --target source-minimal \
  &> image.log &
tail -f image.log

# 推理：
nohup uv run swebench-infer llm_config.json \
    --dataset princeton-nlp/SWE-bench_Verified \
    --split test \
    --max-iterations 500 \
    --num-workers 100 \
    --workspace docker \
    &> infer.log &
tail -f infer.log | more

# 进度
cat $(find eval_outputs -name output.jsonl) | wc -l

# 评测：
nohup uv run swebench-eval $(find eval_outputs -name output.jsonl) \
    --dataset princeton-nlp/SWE-bench_Verified \
    --output-file runid/results.swebench.jsonl \
    --workers 50 \
    --run-id runid --no-modal \
    &> eval.log &
tail -f eval.log





