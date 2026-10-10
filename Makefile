# Tarsus (was omnitool) release Makefile. Python 3.10+ (pytest), Node 22+ (node:test).
# `make test` passes on a clean checkout: nothing excluded from the release is needed.
PY ?= python3
ORT_DIR ?= third_party/onnxruntime
JSON_INC ?= third_party/nlohmann/include
BUILD = build

.PHONY: test test-guard test-py test-mcp interceptor clean

test: test-guard test-py test-mcp

# deterministic safety-guard selftest (62 cases incl. human-approval A4)
test-guard:
	$(PY) bin/omnitool_guard.py selftest

# pytest suite; classifier tests SKIP without sentence-transformers, interceptor tests SKIP without its socket
test-py:
	cd tests && $(PY) -m pytest -q -rs -p no:cacheprovider

# node:test suite; the MCP tool tests need `npm install` in mcp/ (SDK) and SKIP cleanly without it
test-mcp:
	@cd mcp && MCP_TEST_MODE=1 node --test --test-reporter=spec test/truncate.test.js
	@if [ -d mcp/node_modules/@modelcontextprotocol/sdk ]; then \
	  cd mcp && MCP_TEST_MODE=1 node --test --test-reporter=spec test/tarsus-server.test.js; \
	else echo "SKIP mcp/test/tarsus-server.test.js: @modelcontextprotocol/sdk not installed (cd mcp && npm install)"; fi

# Optional, experimental C++ interceptor. Needs ONNX Runtime 1.18.x and nlohmann/json 3.11.x (README "Build").
interceptor: $(BUILD)/omnitool_bert_server
$(BUILD)/omnitool_bert_server: interceptor/omnitool_bert_server.cpp
	mkdir -p $(BUILD)
	g++ -O3 -std=c++17 -I$(ORT_DIR)/include -I$(JSON_INC) $< -L$(ORT_DIR)/lib -lonnxruntime -lpthread \
	    -Wl,-rpath,$(abspath $(ORT_DIR)/lib) -o $@

clean:
	rm -rf $(BUILD) bin/__pycache__ tests/__pycache__ bin/omnibreak.db
