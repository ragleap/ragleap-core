# ZONOS3-Mini: licence notes (read 2026-11-19, not a verification)

ZONOS3-Mini is not verified and not a default (see benchmarks/voice/README.md for the one measurement made). This note only records what was read about its licences, so that nobody has to read it again.

## What was read

- Where: github.com/Zyphra/ZONOS3-Mini at commit 96d9e0ec1e39083ed336ac78f24e3dbc93721c0f, the commit used for the smoke run. The weights licence (Apache-2.0) and the code licence (MIT) are those stated on the model card and in the repository; they are the ones a manifest entry would carry.
- The repository has a LICENSE, a NOTICE, and the folder third_party/licenses with the licence texts of third-party components (named there: fusealign-multilingual, mini-sglang, mlx-lm, nemo, sacremoses, z-speech-alignment and unicode-3.0).
- NOTICE item 4: the Unicode Common Locale Data Repository (CLDR), release 49, copyright Unicode, Inc., SPDX identifier Unicode-3.0, described as the source of number vocabularies, morphology and lexical tables of added locale grammars in zyphra-textnorm. The full text is in third_party/licenses/unicode-3.0.txt.
- The Unicode License V3 text was read at the SPDX licence list page and a copy in another project. It allows free use, copying, modification and distribution of the data files and software; the condition is that the copyright and permission notice appear with all copies or in the documentation; the name of the copyright holder may not be used to promote products without permission; there is no warranty. This is a reading of the text, not legal advice.

## How it relates to the allowlist

The allowlist (licences.py) is applied to the code and weights licences of a manifest entry (api.py raises LicenceError, pull.py refuses, criterion 4 of verification-criteria.md). Unicode-3.0 here is the licence of data inside a component, listed in a NOTICE, so it does not trip that check. It would fail only if it were written as the code or weights licence of an entry. Before this note no file of the voice package or of benchmarks/voice mentioned Unicode.

## Proposed handling (a proposal, not a decision)

1. Do not add Unicode-3.0 to the allowlist of model licences.
2. When a model is verified, read every third-party licence in its NOTICE with the rule of criterion 5 (read at the source, with date and link). Data licences that only require keeping a notice (MIT, BSD, ISC, Unicode-3.0 are examples) would be acceptable in an optional component; a copyleft, non-commercial or field-of-use clause would block it.
3. If the component is ever redistributed (a package, a worker image, an installer), carry its NOTICE and the Unicode licence text and list them in THIRD_PARTY_NOTICES.md. Nothing is redistributed today.

## Not read

NOTICE items 1 to 3 and those after item 4; the licences vendored under servers/ggml/vendor (lmdb, json-schema-validator); the texts in third_party/licenses other than the Unicode one; the licence files of the submodules at their own repositories; whether the CLDR data is part of the weights repository or only of the code repository; the Unicode character data in servers/ggml/src/unicode-data.h, which NOTICE item 4 does not name.

## Update 2026-11-20: the whole NOTICE and the licence files (read at commit 96d9e0ec1e)

The NOTICE (82 lines) lists seven components:

1. Mini-SGLang, MIT: the SGLang-based server under servers/sgl, not the ggml server used in the measurements.
2. NVIDIA NeMo-text-processing, Apache-2.0 (portions copyright Google): ported in zyphra-textnorm (Rust), which is linked into the ggml server.
3. sacremoses, MIT: ported in zyphra-textnorm.
4. Unicode CLDR release 49, Unicode-3.0: data of the added locale grammars in zyphra-textnorm.
5. mlx-lm, MIT: a Metal kernel of the MLX server (servers/mlx).
6. FuseAlign (fusealign-multilingual), Apache-2.0: the forced aligner of speech editing, a git submodule; it was not checked out in the runs of benchmarks/voice.
7. z-speech-alignment, MIT: two functions copied into the Python model code (model/src/zonos3/tts/textmatch.py).

The repository LICENSE is MIT (Zyphra). The files in third_party/licenses are the MIT, Apache-2.0 and Unicode V3 texts named above; only their first lines were read. Where each component is used is taken from the paths named in the NOTICE and was not checked in a built binary.

Vendored in the ggml server and not listed in the NOTICE: servers/ggml/vendor/lmdb/LICENSE begins "The OpenLDAP Public License, Version 2.8, 17 August 2003" (SPDX OLDAP-2.8), which is not on the allowlist; only its first lines were read, so what it asks of someone who redistributes a binary containing it is not recorded here. servers/ggml/vendor/json-schema-validator/LICENSE is the MIT licence (first lines read). The vendor folder also holds httplib.h and json.hpp, whose licences were not read.

Among the texts read, no copyleft, non-commercial or field-of-use clause was seen. This is a reading of texts, not legal advice.

How it relates to the allowlist: the allowlist (licences.py) is applied to the code and weights licences written in a manifest entry, so it does not see components inside a server binary that the operator builds, such as LMDB under OLDAP-2.8. The Kokoro entry in models.toml already carries a comment about its GPLv3 espeak-ng dependency; the same handling is proposed here (a comment in the entry and a line in the documentation), and whether OLDAP-2.8 in an operator-built binary is acceptable is a question for the owner. Nothing in this repository would contain it.

Still not read: the licence of the ggml submodule, the NOTICE and licenses folder of zyphra-textnorm (which reproduces items 2 to 4), httplib.h and json.hpp, the rest of the OLDAP-2.8 text, the committed studio build and the libraries bundled in it, and the terms of the training data behind the weights.
