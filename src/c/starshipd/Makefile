CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -O2 -D_GNU_SOURCE
TARGET = starshipd

.PHONY: all clean test

all: $(TARGET)

$(TARGET): starshipd.c
	$(CC) $(CFLAGS) -o $@ $<

test: $(TARGET)
	./$(TARGET) --once --agent proxy | grep -q 'starship.agent.proxy'
	./$(TARGET) --once --agent ergo | grep -q 'agnetic.agent.ergo'
	@echo "starshipd tests OK"

clean:
	rm -f $(TARGET)
