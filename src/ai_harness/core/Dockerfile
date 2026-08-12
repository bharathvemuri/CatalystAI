# Base image for harness ticket containers (spec section 10).
#
# ubuntu:latest with no further assumptions, per resolved open question #8.
# What goes in here is only what the harness itself needs to operate: git and
# gh for source control, the security scanners the Security agent's contract
# requires, tree-sitter for the context index, and the harness runtime.
#
# Project dependencies are deliberately NOT baked in. They are installed by the
# bootstrap step from whatever the target repository declares, fresh on every
# run — "never assume tools exist, always install" is only safe because this is
# a sandbox, and only honest if the sandbox starts empty.

FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git build-essential \
        python3 python3-pip python3-venv \
        nodejs npm \
        jq \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/local/bin/python

# GitHub CLI, from GitHub's own apt repository.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Security tooling the Security agent's contract requires, and the dependency
# scanner for Python. A missing scanner is a blocking evidence gap, so these are
# part of the image rather than something a gate hopes to find.
RUN pip install --no-cache-dir semgrep pip-audit ruff pytest

# tree-sitter for the context index (spec section 9): language-agnostic,
# offline, no external service.
RUN pip install --no-cache-dir tree-sitter tree-sitter-language-pack

# The harness runtime itself, so gate scripts and agent tooling can call it.
COPY . /opt/ai-harness/core
RUN pip install --no-cache-dir ai-harness || true

WORKDIR /workspace
