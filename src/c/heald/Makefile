CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -O2 -D_GNU_SOURCE
TARGET = heald

.PHONY: all clean test

all: $(TARGET)

$(TARGET): heald.c
	$(CC) $(CFLAGS) -o $@ $<

test: $(TARGET)
	./$(TARGET) --once --check heald | grep -q 'OK\|DOWN'
	./$(TARGET) --once --check nats-server
	@echo "heald tests OK"

clean:
	rm -f $(TARGET)
