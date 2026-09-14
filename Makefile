# Build the girax1 library and the x1 program.
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

# The library headers are reached as <girax1/...>, so the directory holding the
# girax1 directory is what goes on the include path.
CXXFLAGS ?= $(CXXSTD) $(WARNINGS) $(OPTIMIZE) -fPIC -Icxx $(PKG_CFLAGS)
LDLIBS = $(PKG_LIBS)

LIB_DIR = cxx/girax1
LIB_SOURCES = $(LIB_DIR)/config.cc $(LIB_DIR)/websocket.cc $(LIB_DIR)/timer.cc \
	      $(LIB_DIR)/project.cc $(LIB_DIR)/stack.cc
LIB_OBJECTS = $(LIB_SOURCES:.cc=.o)
LIB_HEADERS = $(LIB_DIR)/error.hh $(LIB_DIR)/config.hh $(LIB_DIR)/websocket.hh \
	      $(LIB_DIR)/timer.hh $(LIB_DIR)/project.hh $(LIB_DIR)/stack.hh
LIBRARY = libgirax1.a
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
