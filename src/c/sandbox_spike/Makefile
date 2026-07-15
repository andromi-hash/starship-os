CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -O2 -D_GNU_SOURCE
TARGET = sandbox_run

.PHONY: all clean test bench

all: $(TARGET)

$(TARGET): sandbox_run.c
	$(CC) $(CFLAGS) -o $@ $<

test: $(TARGET)
	./$(TARGET) --timeout 2 -- /bin/echo "starship-sandbox-ok"
	@echo "expect deny:"
	-./$(TARGET) --timeout 1 -- mount 2>/dev/null; test $$? -eq 126

bench: $(TARGET)
	@bash ../../scripts/bench-sandbox.sh 100

clean:
	rm -f $(TARGET)
