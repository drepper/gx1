# Build the girax1 library and the x1 program.
#
# The library talks to a Gira X1 over the Device Stack API, a WebSocket
# protocol carried over TLS.  GnuTLS provides the transport, toml++ reads the
# configuration and jsoncpp handles the messages.
#
# Nothing is written next to the sources: every generated file lands under the
# build directory, which mirrors the layout below cxx.

# GCC is used by default because the C++26 features below need a recent one; an
# override on the command line still wins.
CXX = g++
CXXSTD = -std=c++26
WARNINGS = -Wall -Wextra -Wpedantic
OPTIMIZE ?= -O2 -g

PKGS = gnutls jsoncpp
PKG_CFLAGS := $(shell pkg-config --cflags $(PKGS))
PKG_LIBS := $(shell pkg-config --libs $(PKGS))

SRC_DIR = cxx
BUILD_DIR = build

# The library headers are reached as <girax1/...>, so the directory holding the
# girax1 directory is what goes on the include path.
CXXFLAGS ?= $(CXXSTD) $(WARNINGS) $(OPTIMIZE) -fPIC -I$(SRC_DIR) $(PKG_CFLAGS)
LDLIBS = $(PKG_LIBS)

LIB_DIR = $(SRC_DIR)/girax1
LIB_SOURCES = $(LIB_DIR)/config.cc $(LIB_DIR)/websocket.cc $(LIB_DIR)/timer.cc \
	      $(LIB_DIR)/project.cc $(LIB_DIR)/stack.cc
LIB_HEADERS = $(LIB_DIR)/error.hh $(LIB_DIR)/config.hh $(LIB_DIR)/websocket.hh \
	      $(LIB_DIR)/timer.hh $(LIB_DIR)/project.hh $(LIB_DIR)/stack.hh
PROGRAM_SOURCES = $(SRC_DIR)/x1.cc

LIB_OBJECTS = $(patsubst $(SRC_DIR)/%.cc,$(BUILD_DIR)/%.o,$(LIB_SOURCES))
PROGRAM_OBJECTS = $(patsubst $(SRC_DIR)/%.cc,$(BUILD_DIR)/%.o,$(PROGRAM_SOURCES))
OBJECT_DIRS = $(sort $(dir $(LIB_OBJECTS) $(PROGRAM_OBJECTS)))

LIBRARY = $(BUILD_DIR)/libgirax1.a
PROGRAM = $(BUILD_DIR)/x1

all: $(PROGRAM)

$(LIBRARY): $(LIB_OBJECTS)
	$(AR) rcs $@ $^

$(PROGRAM): $(PROGRAM_OBJECTS) $(LIBRARY)
	$(CXX) $(CXXFLAGS) -o $@ $(PROGRAM_OBJECTS) $(LIBRARY) $(LDLIBS)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cc $(LIB_HEADERS) | $(OBJECT_DIRS)
	$(CXX) $(CXXFLAGS) -c -o $@ $<

# Order-only, so that touching a directory does not rebuild what is in it.
$(OBJECT_DIRS):
	mkdir -p $@

clean:
	$(RM) -r $(BUILD_DIR)

.PHONY: all clean
