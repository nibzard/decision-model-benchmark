"""Opt-in evaluation suites with disjoint validation and test inputs."""

from __future__ import annotations

import csv
import hashlib
import json
import random
from pathlib import Path

from .items import DMB_SEED, DecisionItem, save_items
from .sources import verified_download

FAMILIES = ("s6_banking77", "s7_clinc150", "s8_nlupp")
EXPANDED_SUITES = [f"{family}_{split}" for family in FAMILIES for split in ("validation", "test")]
SOURCE_FILE = Path(__file__).with_name("expanded_sources.json")


def text_hash(text: str) -> str:
    """Normalize whitespace and case to prevent cross-split text leakage."""
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()


def disjoint_validation(validation: list[DecisionItem], test: list[DecisionItem]):
    test_texts = {item.meta["source_text_sha256"] for item in test}
    return [item for item in validation if item.meta["source_text_sha256"] not in test_texts]


def choice_item(family, split, index, text, label, options, **metadata):
    key = f"{family}_{split}"
    return DecisionItem(
        item_id=f"{key}-{index:05d}",
        suite=key,
        state=text,
        options=list(options),
        gold_index=options.index(label),
        kind="intent",
        meta={"split": split, "source_text_sha256": text_hash(text), **metadata},
    )


def banking_items(train: list[dict], test: list[dict]) -> dict[str, list[DecisionItem]]:
    labels = sorted({row["category"] for row in train})
    result = {
        "test": [
            choice_item(
                "s6_banking77", "test", i, row["text"], row["category"], labels, source_split="test"
            )
            for i, row in enumerate(test)
        ]
    }
    test_texts = {text_hash(row["text"]) for row in test}
    groups = {label: [] for label in labels}
    seen = set(test_texts)
    for row in train:
        digest = text_hash(row["text"])
        if digest not in seen:
            groups[row["category"]].append(row)
            seen.add(digest)
    rng = random.Random(f"{DMB_SEED}:banking77-validation")
    validation = []
    for label, group in sorted(groups.items()):
        rng.shuffle(group)
        if len(group) < 10:
            raise ValueError(f"not enough disjoint validation examples for {label}")
        validation.extend(group[:10])
    result["validation"] = [
        choice_item(
            "s6_banking77",
            "validation",
            i,
            row["text"],
            row["category"],
            labels,
            source_split="train",
        )
        for i, row in enumerate(validation)
    ]
    return result


def clinc_items(data: dict) -> dict[str, list[DecisionItem]]:
    labels = sorted({label for _, label in data["train"]})
    if "oos" in labels:
        raise ValueError("CLINC training intents must not contain oos")
    options = labels + ["out_of_scope: none of the listed intents applies"]
    result = {}
    for split, source in (("validation", "val"), ("test", "test")):
        result[split] = [
            choice_item(
                "s7_clinc150",
                split,
                i,
                text,
                options[-1] if label == "oos" else label,
                options,
                out_of_scope=label == "oos",
                source_split=source,
            )
            for i, (text, label) in enumerate(data[source] + data[f"oos_{source}"])
        ]
    result["validation"] = disjoint_validation(result["validation"], result["test"])
    return result


def nlupp_items(ontology: dict, folds: dict[tuple[str, int], list[dict]]):
    result = {"validation": [], "test": []}
    for (domain, fold), rows in sorted(folds.items()):
        split = "validation" if fold in (16, 17) else "test"
        if fold not in (16, 17, 18, 19):
            raise ValueError("NLU++ evaluation uses folds 16 through 19 only")
        intents = {
            name: spec
            for name, spec in sorted(ontology["intents"].items())
            if domain in spec["domain"] or "general" in spec["domain"]
        }
        for index, row in enumerate(rows):
            gold = set(row.get("intents", []))
            if not gold <= intents.keys():
                raise ValueError(
                    f"NLU++ labels missing from {domain} ontology: {gold - intents.keys()}"
                )
            base = f"nlupp-{domain}-{fold}-{index:04d}"
            for label, spec in intents.items():
                item = choice_item(
                    "s8_nlupp",
                    split,
                    len(result[split]),
                    row["text"],
                    "yes" if label in gold else "no",
                    ["no", "yes"],
                    domain=domain,
                    source_fold=fold,
                    intent=label,
                    group_size=len(intents),
                )
                item.state = json.dumps(
                    {
                        "message": row["text"],
                        "question": spec["description"],
                        "instruction": "Does this intent apply to the message? Choose yes or no.",
                    },
                    ensure_ascii=False,
                )
                item.kind = "multi_label"
                item.base_item_id = base
                result[split].append(item)
    result["validation"] = disjoint_validation(result["validation"], result["test"])
    return result


ATTRIBUTION = """# Expanded suite sources

- Banking77: Casanueva et al. (2020), PolyAI, CC BY 4.0.
  https://github.com/PolyAI-LDN/task-specific-datasets
  S6 preserves the official test set. Validation samples ten unique training
  texts per intent after excluding normalized test texts.
- CLINC150: Larson et al. (2019), CC BY 3.0.
  https://github.com/clinc/oos-eval
  S7 uses official validation and test splits, including out-of-scope queries.
- NLU++: Casanueva et al. (2022), PolyAI, CC BY 4.0.
  https://github.com/PolyAI-LDN/task-specific-datasets/tree/master/nlupp
  S8 adapts the intent task into one binary Choice per applicable intent.
  Validation uses folds 16–17; test uses folds 18–19 in both domains.
  All labels for a message stay in the same split. Slot extraction is excluded.

Validation removes texts that also appear in test after case and whitespace
normalization. Test examples remain intact. NLU++ is an adapted fixed split,
not the dataset's published cross-validation score. Native Noul outputs and
multiple questions per API request are not measured by this Choice protocol.
Exact source commits, byte hashes, split counts and item hashes are in
expanded-hashes.json. The original five suites retain their existing files.
"""


def build_expanded(root: Path) -> dict:
    sources = json.loads(SOURCE_FILE.read_text())
    raw = root / "data/raw/expanded"
    paths = {name: verified_download(raw / name, source) for name, source in sources.items()}

    def read_csv(name):
        with paths[name].open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    families = {
        "s6_banking77": banking_items(
            read_csv("banking77-train.csv"), read_csv("banking77-test.csv")
        ),
        "s7_clinc150": clinc_items(json.loads(paths["clinc150.json"].read_text())),
        "s8_nlupp": nlupp_items(
            json.loads(paths["nlupp-ontology.json"].read_text()),
            {
                (domain, fold): json.loads(paths[f"nlupp-{domain}-{fold}.json"].read_text())
                for domain in ("banking", "hotels")
                for fold in (16, 17, 18, 19)
            },
        ),
    }
    manifest = {
        "seed": DMB_SEED,
        "sources": sources,
        "suites": {},
        "split_policy": {
            "banking77": "10 unique training texts per intent; all official test records",
            "clinc150": "official val/oos_val and test/oos_test",
            "nlupp": "validation folds 16–17; test folds 18–19; banking and hotels",
            "overlap": "exclude validation text matching test after case/whitespace normalization",
        },
    }
    for family, splits in families.items():
        validation_hashes = {i.meta["source_text_sha256"] for i in splits["validation"]}
        test_hashes = {i.meta["source_text_sha256"] for i in splits["test"]}
        if validation_hashes & test_hashes:
            raise ValueError(f"validation/test leakage in {family}")
        for split, items in splits.items():
            key = f"{family}_{split}"
            digest = save_items(items, root / f"data/suites/{key}.jsonl")
            manifest["suites"][key] = {
                "items": len(items),
                "messages": len({i.base_item_id or i.item_id for i in items}),
                "sha256": digest,
                "split": split,
            }
            print(f"built {key}: {len(items)} decisions; sha256={digest[:16]}")
    target = root / "data/suites/expanded-hashes.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (root / "data/EXPANDED-LICENSES.md").write_text(ATTRIBUTION)
    return manifest
