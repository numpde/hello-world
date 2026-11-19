# Only repository/image/acquire TEST_RUNNER=parallel may run this networked build.
# Python itself comes from the same digest-selected serial checker base.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

# Exact upstream wheel URLs and SHA-256 fragments select immutable artifacts.
# unittest-parallel 1.8.6 imports coverage even without the --coverage option.
RUN python3 -m pip install --disable-pip-version-check --no-cache-dir --no-deps \
    'https://files.pythonhosted.org/packages/d1/8e/f7d7e69e2ad34ce08595b787eb578f669c00fd57d432ed4c7cfd71666f36/coverage-7.17.3-py3-none-any.whl#sha256=21745c08f2835b847a16887b748d3084dddeb0571ae2a0d62e33644413568b99' \
    'https://files.pythonhosted.org/packages/45/4c/8d1732d73eb2399f1e5a5097ae30dbdf85da12c2da6d0135b22748333fe9/unittest_parallel-1.8.6-py3-none-any.whl#sha256=7f04b0ada502f6b3d655ef49ea2a785f0e37869f9d0ce7a075c1a18ec204ac93' \
    && python3 -m pip check \
    && unittest-parallel --help >/dev/null

ENTRYPOINT []
CMD ["python3", "--version"]
