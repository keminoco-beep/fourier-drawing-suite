"""LoRA fine-tuning for the local FDSL planner (Qwen2.5-1.5B-Instruct).

The script is **offline only** (``local_files_only=True``) and is safe to import
without torch/transformers/peft: the heavy imports are guarded so that
``--help``, argument validation and ``--dry-run`` work on a CPU-only install.
Actual training requires the GPU stack that the Lead installs.

Examples::

    python scripts/train_lora.py --dry-run
    python scripts/train_lora.py --smoke --max-steps 20 \
        --dataset out/llm/data/train.jsonl --output out/llm/qwen2.5-1.5b-lora-v1
    python scripts/train_lora.py --num-epochs 1 --batch-size 1 --grad-accum 8

Default hyper-parameters follow KICKOFF v8 §3: LoRA r=16 alpha=32 dropout=0.05
on q/k/v/o/gate/up/down, bf16 (fp16 fallback), batch 1, grad-accum 8, lr 1e-4,
cosine schedule, 20 warmup steps, max_len 4096, 1 epoch.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fourier_prompt.llm.dataset import load_jsonl  # noqa: E402
from fourier_prompt.llm.formatting import (  # noqa: E402
    canonical_plan_json,
    tokenize_example,
)

try:  # Heavy optional stack: guarded so --help/--dry-run work without it.
    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        get_cosine_schedule_with_warmup,
    )
    from peft import LoraConfig, PeftModel, get_peft_model

    _IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - depends on the environment
    torch = None
    DataLoader = None
    # Keep the Dataset base usable so the module (and its CLI) imports cleanly
    # even when the GPU stack is missing or partially broken.
    Dataset = object
    AutoModelForCausalLM = AutoTokenizer = None
    get_cosine_schedule_with_warmup = None
    LoraConfig = PeftModel = get_peft_model = None
    _IMPORT_ERROR = _exc

DEFAULT_TARGETS = "q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
DEFAULT_MODEL = os.environ.get(
    "FDP_LLM_MODEL", str(_ROOT.parent / "models" / "Qwen2.5-1.5B-Instruct")
)
DEFAULT_DATASET = str(_ROOT / "out" / "llm" / "data" / "train.jsonl")
DEFAULT_OUTPUT = str(_ROOT / "out" / "llm" / "qwen2.5-1.5b-lora-v1")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python scripts/train_lora.py",
        description="LoRA fine-tuning for the local Qwen2.5 FDSL planner (offline).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="local base model directory")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="training JSONL split")
    parser.add_argument(
        "--val-dataset", default=None, help="validation JSONL (default: sibling val.jsonl)"
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="adapter output directory")
    parser.add_argument("--num-epochs", type=float, default=1.0, help="epochs (1-2 recommended)")
    parser.add_argument("--max-steps", type=int, default=-1, help="optimizer steps (-1 = all)")
    parser.add_argument("--batch-size", type=int, default=1, help="micro batch size (1-2)")
    parser.add_argument("--grad-accum", type=int, default=8, help="gradient accumulation")
    parser.add_argument("--lr", type=float, default=1e-4, help="learning rate")
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--warmup-steps", type=int, default=20)
    parser.add_argument("--max-len", type=int, default=4096, help="max sequence length")
    parser.add_argument("--lora-r", type=int, default=16, help="LoRA rank (16 or 32)")
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument(
        "--target-modules",
        default=DEFAULT_TARGETS,
        help="comma separated LoRA target modules",
    )
    parser.add_argument(
        "--precision",
        choices=("auto", "bf16", "fp16", "no"),
        default="auto",
        help="auto = bf16 when supported, else fp16; 'no' = fp32",
    )
    parser.add_argument(
        "--gradient-checkpointing",
        action="store_true",
        help="trade compute for VRAM (useful for longer sequences)",
    )
    parser.add_argument("--save-steps", type=int, default=200)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue from an existing adapter in --output",
    )
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="smoke run: --max-steps 20 when --max-steps is not given",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved config/steps and exit (no torch required)",
    )
    return parser


def _target_modules(args):
    return [item.strip() for item in str(args.target_modules).split(",") if item.strip()]


def validate_args(parser, args):
    """Validate hyper-parameters; ``parser.error`` exits with status 2."""
    if args.batch_size < 1 or args.batch_size > 64:
        parser.error("--batch-size must be in [1, 64]")
    if args.grad_accum < 1 or args.grad_accum > 256:
        parser.error("--grad-accum must be in [1, 256]")
    if not (0.0 < args.lr <= 1.0):
        parser.error("--lr must be in (0, 1]")
    if args.weight_decay < 0.0:
        parser.error("--weight-decay must be >= 0")
    if args.warmup_steps < 0:
        parser.error("--warmup-steps must be >= 0")
    if args.max_len < 128 or args.max_len > 32768:
        parser.error("--max-len must be in [128, 32768]")
    if args.max_steps != -1 and args.max_steps < 1:
        parser.error("--max-steps must be -1 or >= 1")
    if args.num_epochs <= 0:
        parser.error("--num-epochs must be > 0")
    if args.lora_r < 1 or args.lora_r > 256:
        parser.error("--lora-r must be in [1, 256]")
    if args.lora_alpha < 1 or args.lora_alpha > 1024:
        parser.error("--lora-alpha must be in [1, 1024]")
    if not (0.0 <= args.lora_dropout < 1.0):
        parser.error("--lora-dropout must be in [0, 1)")
    if args.save_steps < 1 or args.logging_steps < 1:
        parser.error("--save-steps/--logging-steps must be >= 1")
    if args.seed < 0:
        parser.error("--seed must be >= 0")
    if args.num_workers < 0:
        parser.error("--num-workers must be >= 0")
    if not _target_modules(args):
        parser.error("--target-modules must name at least one module")
    return True


# ---------------------------------------------------------------------------
# Resolved configuration (used by --dry-run and train())
# ---------------------------------------------------------------------------


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def _resolve_val_dataset(args):
    if args.val_dataset:
        return args.val_dataset
    candidate = os.path.join(os.path.dirname(os.path.abspath(args.dataset)), "val.jsonl")
    return candidate if os.path.isfile(candidate) else None


def resolved_config(args):
    train_lines = _count_lines(args.dataset)
    val_path = _resolve_val_dataset(args)
    val_lines = _count_lines(val_path) if val_path else None
    micro_per_epoch = (
        math.ceil(train_lines / args.batch_size) if train_lines else None
    )
    if args.max_steps > 0:
        planned_steps = args.max_steps
    elif micro_per_epoch:
        planned_steps = math.ceil(micro_per_epoch / args.grad_accum * args.num_epochs)
    else:
        planned_steps = None
    if args.precision == "no":
        planned_precision = "fp32"
    elif args.precision == "auto":
        planned_precision = "bf16-if-supported-else-fp16"
    else:
        planned_precision = args.precision
    return {
        "model": os.path.abspath(args.model),
        "dataset": os.path.abspath(args.dataset),
        "val_dataset": os.path.abspath(val_path) if val_path else None,
        "output": os.path.abspath(args.output),
        "train_records": train_lines,
        "val_records": val_lines,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "effective_batch": args.batch_size * args.grad_accum,
        "num_epochs": args.num_epochs,
        "max_steps": args.max_steps,
        "planned_optimizer_steps": planned_steps,
        "lr": args.lr,
        "warmup_steps": args.warmup_steps,
        "max_len": args.max_len,
        "lora": {
            "r": args.lora_r,
            "alpha": args.lora_alpha,
            "dropout": args.lora_dropout,
            "target_modules": _target_modules(args),
        },
        "precision": planned_precision,
        "gradient_checkpointing": bool(args.gradient_checkpointing),
        "resume": bool(args.resume),
        "seed": args.seed,
        "torch_available": _IMPORT_ERROR is None,
        "import_error": None if _IMPORT_ERROR is None else str(_IMPORT_ERROR),
    }


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


class PlanJsonlDataset(Dataset):
    """Lazy tokenised dataset over ``build_dataset`` JSONL records."""

    def __init__(self, path, tokenizer, max_len):
        self.path = str(path)
        self.tokenizer = tokenizer
        self.max_len = int(max_len)
        self.records = load_jsonl(path)
        if not self.records:
            raise ValueError(f"dataset is empty: {path}")

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        record = self.records[index]
        plan_json = record.get("plan_json")
        if not isinstance(plan_json, str) or not plan_json.strip():
            plan_json = canonical_plan_json(record["plan"])
        return tokenize_example(
            self.tokenizer,
            record["prompt"],
            plan_json,
            max_len=self.max_len,
        )


class PadCollator:
    """Pad ``input_ids``/``labels`` to the longest sequence in the batch."""

    def __init__(self, pad_token_id, *, pad_to_multiple_of=8):
        self.pad_token_id = int(pad_token_id)
        self.pad_to_multiple_of = int(pad_to_multiple_of)

    def __call__(self, features):
        max_len = max(len(item["input_ids"]) for item in features)
        multiple = max(1, self.pad_to_multiple_of)
        max_len = ((max_len + multiple - 1) // multiple) * multiple
        input_ids, attention, labels = [], [], []
        for item in features:
            pad = max_len - len(item["input_ids"])
            input_ids.append(list(item["input_ids"]) + [self.pad_token_id] * pad)
            attention.append(list(item["attention_mask"]) + [0] * pad)
            labels.append(list(item["labels"]) + [-100] * pad)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def _resolve_dtype(args):
    if args.precision == "no":
        return torch.float32, "fp32"
    if args.precision == "bf16":
        return torch.bfloat16, "bf16"
    if args.precision == "fp16":
        return torch.float16, "fp16"
    use_cuda = torch.cuda.is_available()
    if use_cuda and getattr(torch.cuda, "is_bf16_supported", lambda: False)():
        return torch.bfloat16, "bf16"
    if use_cuda:
        return torch.float16, "fp16"
    return torch.float32, "fp32"


def _load_model_and_tokenizer(args, dtype):
    model_dir = os.path.abspath(args.model)
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"local model directory not found: {model_dir}")
    tokenizer = AutoTokenizer.from_pretrained(
        model_dir, local_files_only=True, trust_remote_code=False
    )
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise ValueError("tokenizer has neither pad_token nor eos_token")
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=dtype,
    )
    model.config.use_cache = False
    if args.gradient_checkpointing:
        if hasattr(model, "gradient_checkpointing_enable"):
            model.gradient_checkpointing_enable()
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()

    adapter_config = os.path.join(os.path.abspath(args.output), "adapter_config.json")
    if args.resume and os.path.isfile(adapter_config):
        model = PeftModel.from_pretrained(model, os.path.abspath(args.output), is_trainable=True)
        print(f"resumed adapter from {os.path.abspath(args.output)}")
    elif args.resume:
        print(
            f"warning: --resume given but no adapter found at {adapter_config}; starting fresh",
            file=sys.stderr,
        )
        model = _wrap_lora(model, args)
    else:
        model = _wrap_lora(model, args)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.train()
    if hasattr(model, "print_trainable_parameters"):
        model.print_trainable_parameters()
    return model, tokenizer, device


def _wrap_lora(model, args):
    config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=_target_modules(args),
        bias="none",
        task_type="CAUSAL_LM",
    )
    return get_peft_model(model, config)


def _evaluate(model, loader, device, amp_dtype, max_batches=50):
    model.eval()
    total, count = 0.0, 0
    use_amp = amp_dtype is not None and str(device).startswith("cuda")
    with torch.no_grad():
        for index, batch in enumerate(loader):
            if index >= max_batches:
                break
            batch = {key: value.to(device) for key, value in batch.items()}
            with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=use_amp):
                output = model(**batch)
            total += float(output.loss.item())
            count += 1
    model.train()
    return total / count if count else None


def train(args):
    started = time.time()
    os.makedirs(os.path.abspath(args.output), exist_ok=True)
    dtype, dtype_name = _resolve_dtype(args)
    model, tokenizer, device = _load_model_and_tokenizer(args, dtype)

    train_dataset = PlanJsonlDataset(args.dataset, tokenizer, args.max_len)
    collator = PadCollator(tokenizer.pad_token_id)
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=collator,
        num_workers=args.num_workers,
    )
    val_path = _resolve_val_dataset(args)
    val_loader = None
    if val_path and os.path.isfile(val_path):
        try:
            val_loader = DataLoader(
                PlanJsonlDataset(val_path, tokenizer, args.max_len),
                batch_size=args.batch_size,
                shuffle=False,
                collate_fn=collator,
                num_workers=args.num_workers,
            )
        except ValueError as exc:
            print(f"warning: validation split unusable: {exc}", file=sys.stderr)

    micro_per_epoch = math.ceil(len(train_dataset) / args.batch_size)
    if args.max_steps > 0:
        total_steps = args.max_steps
    else:
        total_steps = max(
            1, math.ceil(micro_per_epoch / args.grad_accum * args.num_epochs)
        )
    trainable = [param for param in model.parameters() if param.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=args.weight_decay)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer,
        num_warmup_steps=min(args.warmup_steps, max(0, total_steps - 1)),
        num_training_steps=total_steps,
    )
    amp_dtype = dtype if dtype_name in ("bf16", "fp16") else None
    scaler = torch.amp.GradScaler("cuda") if dtype_name == "fp16" and device == "cuda" else None

    print(
        f"device={device} dtype={dtype_name} train={len(train_dataset)} "
        f"steps={total_steps} batch={args.batch_size}x{args.grad_accum}"
    )
    history = []
    global_step = 0
    micro_step = 0
    running_loss = 0.0
    window_loss = 0.0
    window_steps = 0
    last_log = time.time()
    optimizer.zero_grad(set_to_none=True)
    use_amp = amp_dtype is not None and str(device).startswith("cuda")

    while global_step < total_steps:
        for batch in train_loader:
            batch = {key: value.to(device) for key, value in batch.items()}
            with torch.autocast(device_type="cuda", dtype=amp_dtype, enabled=use_amp):
                output = model(**batch)
                loss = output.loss / args.grad_accum
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            running_loss += float(loss.item())
            micro_step += 1
            if micro_step % args.grad_accum != 0:
                continue

            if scaler is not None:
                scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            if scaler is not None:
                scaler.step(optimizer)
                scaler.update()
            else:
                optimizer.step()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
            global_step += 1
            window_loss += running_loss
            window_steps += 1
            running_loss = 0.0

            if global_step % args.logging_steps == 0 or global_step == 1:
                mean_loss = window_loss / max(1, window_steps)
                entry = {
                    "step": global_step,
                    "loss": round(mean_loss, 5),
                    "lr": round(scheduler.get_last_lr()[0], 8),
                    "elapsed_s": round(time.time() - started, 1),
                }
                history.append(entry)
                print(
                    f"step {global_step}/{total_steps} loss={entry['loss']:.4f} "
                    f"lr={entry['lr']:.2e} ({entry['elapsed_s']:.0f}s, "
                    f"{time.time() - last_log:.1f}s/interval)"
                )
                window_loss = 0.0
                window_steps = 0
                last_log = time.time()

            if global_step % args.save_steps == 0:
                _save(args, model, tokenizer, history, global_step, dtype_name, device)
                if val_loader is not None:
                    eval_loss = _evaluate(model, val_loader, device, amp_dtype)
                    print(f"step {global_step}: eval_loss={eval_loss}")

            if global_step >= total_steps:
                break

    _save(args, model, tokenizer, history, global_step, dtype_name, device)
    print(f"done: {global_step} steps in {time.time() - started:.0f}s -> {args.output}")
    return 0


def _save(args, model, tokenizer, history, global_step, dtype_name, device):
    output = os.path.abspath(args.output)
    os.makedirs(output, exist_ok=True)
    model.save_pretrained(output)
    tokenizer.save_pretrained(output)
    metadata = {
        "base_model": os.path.abspath(args.model),
        "step": global_step,
        "dtype": dtype_name,
        "device": device,
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "lora_dropout": args.lora_dropout,
        "target_modules": _target_modules(args),
        "max_len": args.max_len,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "lr": args.lr,
        "history": history[-200:],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(output, "train_meta.json"), "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"saved adapter to {output}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.smoke and args.max_steps == -1:
        args.max_steps = 20
    validate_args(parser, args)

    if args.dry_run:
        config = resolved_config(args)
        print(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True))
        if config["train_records"] is None:
            print(
                f"warning: dataset not found yet: {config['dataset']}",
                file=sys.stderr,
            )
        return 0

    if _IMPORT_ERROR is not None:
        print(
            "error: torch/transformers/peft are not available; install them or run "
            f"--dry-run. Import error: {type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}",
            file=sys.stderr,
        )
        return 2

    if not os.path.isfile(args.dataset):
        print(f"error: dataset not found: {args.dataset}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.model):
        print(f"error: model directory not found: {args.model}", file=sys.stderr)
        return 2

    try:
        return train(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: training failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
