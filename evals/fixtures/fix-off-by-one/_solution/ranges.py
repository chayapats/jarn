def chunk(lst, n):
    """Split lst into sublists of size n (last chunk may be smaller)."""
    return [lst[i:i+n] for i in range(0, len(lst), n)]
