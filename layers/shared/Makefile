# Layer build, driven by SAM's `BuildMethod: makefile`.
#
# Why this exists rather than `BuildMethod: python3.12`. That builder treats
# ContentUri as a Python project root: it copies the source tree *and* pip-installs
# into a `python/` directory. Our source already keeps the package at
# `python/nexus_common`, so the copy landed it at `python/python/nexus_common`.
# Lambda puts `/opt/python` on sys.path, so every function deployed fine and then
# died at import with `No module named 'nexus_common'` — one directory too deep.
#
# Laying the artifact out explicitly is the fix: dependencies and the shared
# package both go directly under `$(ARTIFACTS_DIR)/python`, which becomes
# `/opt/python` at runtime.
#
# The platform is pinned rather than inherited. `use_container = true` does put the
# build in a Linux container, but with BuildMethod: makefile SAM picked the default
# x86_64 image while the functions declare `Architectures: [arm64]` — so psycopg and
# numpy installed as `cp312-manylinux2014_x86_64` and every function died at import
# with "no pq wrapper available", which reads like a broken psycopg rather than a
# wrong CPU. Naming the target platform makes the artifact correct no matter which
# image or host runs this, which is also what makes the build reproducible in CI.
#
# `--platform` requires `--only-binary=:all:`; every dependency here publishes an
# aarch64 wheel, so nothing needs to compile.

LAYER_PLATFORM = manylinux2014_aarch64
LAYER_PYTHON   = 3.12

build-SharedLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	python3 -m pip install --quiet --requirement requirements.txt \
		--target "$(ARTIFACTS_DIR)/python" \
		--platform $(LAYER_PLATFORM) --python-version $(LAYER_PYTHON) \
		--implementation cp --only-binary=:all: --upgrade
	cp -r python/nexus_common "$(ARTIFACTS_DIR)/python/"
