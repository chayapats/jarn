from ranges import chunk

def test_chunk_with_partial():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

def test_chunk_exact():
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
