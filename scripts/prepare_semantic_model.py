"""Explicit one-time model download. Never opens ST databases or sends memories."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.semantic_retrieval import MODEL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, required=True)
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    if cache == root or root in cache.parents:
        parser.error('Keep model files outside the source repository')
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=MODEL, cache_dir=str(cache), threads=2,
                          providers=['CPUExecutionProvider'])
    directory = Path(model.model._model_dir).resolve()
    # Warm up using a synthetic phrase, not a real record.
    vector = next(iter(model.query_embed('合成测试：灯突然不亮了')))
    print('Model:', MODEL)
    print('Dimensions:', len(vector))
    print('STBRAIN_SEMANTIC_MODEL_DIR=' + str(directory))
    print('Set STBRAIN_SEMANTIC_INDEX_DIR to a separate private data directory.')


if __name__ == '__main__':
    main()
