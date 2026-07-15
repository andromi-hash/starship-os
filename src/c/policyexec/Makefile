CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -O2 -D_GNU_SOURCE
TARGET = policyexec

.PHONY: all clean test

all: $(TARGET)

$(TARGET): policyexec.c
	$(CC) $(CFLAGS) -o $@ $<

POLICY ?= $(abspath $(CURDIR)/../../config/policy.default.json)

test: $(TARGET)
	@echo "== list =="
	./$(TARGET) --policy $(POLICY) list
	@echo "== deny opencode =="
	./$(TARGET) --policy $(POLICY) check-tool opencode; test $$? -eq 1
	@echo "== allow read_file =="
	./$(TARGET) --policy $(POLICY) check-tool read_file; test $$? -eq 0
	@echo "== block mount =="
	./$(TARGET) --policy $(POLICY) check-command mount; test $$? -eq 1
	@echo "== allow echo =="
	./$(TARGET) --policy $(POLICY) check-command /bin/echo; test $$? -eq 0
	@echo "== red-team deny shell =="
	./$(TARGET) --policy $(POLICY) --role red-team check-tool shell; test $$? -eq 1
	@echo "== red-team allow read_file =="
	./$(TARGET) --policy $(POLICY) --role red-team check-tool read_file; test $$? -eq 0
	@echo "policyexec tests OK"

clean:
	rm -f $(TARGET)
