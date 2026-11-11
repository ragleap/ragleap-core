# Changelog

All notable changes to ragleap-phone are recorded here, in the Keep a Changelog style.

## [Unreleased]

### Added
- Package skeleton: `Frame`, a frozen audio frame (PCM 16-bit little-endian, mono) that carries its own sample rate and timestamp, as proposed in docs/design/phase0-adapter-mapping.md. No backends, no calls, nothing released.
- Adapter interfaces from docs/design/phase0-interface.md: `Adapter`, `Call`, `Capabilities` and `Event` (abstract, no backend), and `new_call_id()`. A call id is always made on our side and cannot be passed in. Tested with a fake adapter; nothing is released.
- `Resampler`: a stateful sample-rate converter for `Frame` objects (16 kHz to and from 8 kHz and 48 kHz), standard library only. It carries copies of the ragleap-voice filters, so there is no dependency; a test compares exact output with ragleap-voice when that is installed. Other rate pairs are refused. Nothing is released.
