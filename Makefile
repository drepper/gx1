# Build the knx library and the x1 program.
#
# The library talks to a Gira X1 over the Device Stack API, a WebSocket
# protocol carried over TLS.  GnuTLS provides the transport, toml++ reads the
# configuration and jsoncpp handles the messages.

# GCC is used by default because the C++26 features below need a recent one; an
# override on the command line still wins.
CXX = g++
CXXSTD = -std=c++26
WARNINGS = -Wall -Wextra -Wpedantic
OPTIMIZE ?= -O2 -g

PKGS = gnutls jsoncpp
PKG_CFLAGS := $(shell pkg-config --cflags $(PKGS))
PKG_LIBS := $(shell pkg-config --libs $(PKGS))

CXXFLAGS ?= $(CXXSTD) $(WARNINGS) $(OPTIMIZE) -fPIC $(PKG_CFLAGS)
LDLIBS = $(PKG_LIBS)

LIB_SOURCES = knx/config.cc knx/websocket.cc knx/timer.cc knx/project.cc knx/stack.cc
LIB_OBJECTS = $(LIB_SOURCES:.cc=.o)
LIB_HEADERS = knx/error.hh knx/config.hh knx/websocket.hh knx/timer.hh knx/project.hh \
	      knx/stack.hh
LIBRARY = libknx.a
PROGRAM = x1

all: $(PROGRAM)

$(LIBRARY): $(LIB_OBJECTS)
	$(AR) rcs $@ $^

$(PROGRAM): x1.o $(LIBRARY)
	$(CXX) $(CXXFLAGS) -o $@ x1.o $(LIBRARY) $(LDLIBS)

%.o: %.cc $(LIB_HEADERS)
	$(CXX) $(CXXFLAGS) -c -o $@ $<

clean:
	$(RM) $(LIB_OBJECTS) x1.o $(LIBRARY) $(PROGRAM)

.PHONY: all clean
