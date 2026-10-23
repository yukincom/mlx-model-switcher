"""Shared Hugging Face cache deletion primitive used after manager guards."""

def delete_cached_model(repo_id: str) -> int:
    from huggingface_hub import scan_cache_dir

    info = scan_cache_dir()
    repo = next((item for item in info.repos if item.repo_type == "model" and item.repo_id == repo_id), None)
    if repo is None:
        return 0
    revisions = [item.commit_hash for item in repo.revisions]
    strategy = info.delete_revisions(*revisions)
    expected = int(strategy.expected_freed_size)
    strategy.execute()
    return expected
