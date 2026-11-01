import ragleap_voice as rv


def test_stream_functions_are_exported():
    assert "transcribe_stream" in rv.__all__ and "synthesize_stream" in rv.__all__
    assert callable(rv.transcribe_stream) and callable(rv.synthesize_stream)


def test_building_blocks_stay_unexported():
    for name in ("Down2Stream", "Up2Stream", "StreamGate"):
        assert name not in rv.__all__
        assert not hasattr(rv, name)


def test_every_name_in_all_exists_once():
    assert len(rv.__all__) == len(set(rv.__all__))
    assert all(hasattr(rv, n) for n in rv.__all__)


def test_transcribe_stream_from_package_runs_with_no_audio():
    s = rv.transcribe_stream(iter([]), gate="off")
    assert [e.type for e in s] == ["stream.started", "stream.no_speech"]
