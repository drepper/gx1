# Build the girax1 library, static and shared, and the x1 program.
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

# The sources are written for C++26 but need no more than C++23, so the newest
# standard the compiler accepts is used and the sources adapt through the
# feature test macros in girax1/compat.hh.  Setting CXXSTD on the command line
# overrides the choice.
STANDARDS = c++26 c++23
WORKING_STANDARDS := $(foreach s,$(STANDARDS),\
	$(shell echo 'int main(){}' | $(CXX) -std=$(s) -x c++ - -o /dev/null 2>/dev/null && echo $(s)))
CXXSTD ?= -std=$(firstword $(WORKING_STANDARDS))

WARNINGS = -Wall -Wextra -Wpedantic

# CXXFLAGS and LDFLAGS are left to whoever builds, as for example rpmbuild sets
# them; what the library itself needs is added separately.
CXXFLAGS ?= -O2 -g
LDFLAGS ?=

PKGS = gnutls jsoncpp
PKG_CFLAGS := $(shell pkg-config --cflags $(PKGS))
PKG_LIBS := $(shell pkg-config --libs $(PKGS))

SRC_DIR = cxx
BUILD_DIR = build

# The library headers are reached as <girax1/...>, so the directory holding the
# girax1 directory is what goes on the include path.
ALL_CXXFLAGS = $(CXXSTD) $(WARNINGS) -fPIC -I$(SRC_DIR) $(PKG_CFLAGS) $(CXXFLAGS)
LDLIBS = $(PKG_LIBS)

# The version is the one of the whole project, kept in girax1/__init__.py.  The
# shared library carries it in its file name, as libgirax1-VERSION.so.  Its
# soname names the interface instead, libgirax1.so.SOVERSION, and only changes
# when SOVERSION is raised, which has to happen whenever a change breaks
# programs built against an earlier version.
VERSION := $(shell sed -n "s/^__version__ = '\(.*\)'$$/\1/p" girax1/__init__.py)
SOVERSION = 1

LIB_DIR = $(SRC_DIR)/girax1
LIB_SOURCES = $(LIB_DIR)/config.cc $(LIB_DIR)/websocket.cc $(LIB_DIR)/timer.cc \
	      $(LIB_DIR)/project.cc $(LIB_DIR)/stack.cc
LIB_HEADERS = $(LIB_DIR)/compat.hh $(LIB_DIR)/error.hh $(LIB_DIR)/config.hh \
	      $(LIB_DIR)/websocket.hh $(LIB_DIR)/timer.hh $(LIB_DIR)/project.hh \
	      $(LIB_DIR)/stack.hh
PROGRAM_SOURCES = $(SRC_DIR)/x1.cc

LIB_OBJECTS = $(patsubst $(SRC_DIR)/%.cc,$(BUILD_DIR)/%.o,$(LIB_SOURCES))
PROGRAM_OBJECTS = $(patsubst $(SRC_DIR)/%.cc,$(BUILD_DIR)/%.o,$(PROGRAM_SOURCES))
OBJECT_DIRS = $(sort $(dir $(LIB_OBJECTS) $(PROGRAM_OBJECTS)))

LIBRARY = $(BUILD_DIR)/libgirax1.a
SONAME = libgirax1.so.$(SOVERSION)
SHARED = $(BUILD_DIR)/libgirax1-$(VERSION).so
PROGRAM = $(BUILD_DIR)/x1

all: $(PROGRAM) lib

lib: $(LIBRARY) $(SHARED)

$(LIBRARY): $(LIB_OBJECTS)
	$(AR) rcs $@ $^

$(SHARED): $(LIB_OBJECTS)
	$(CXX) $(ALL_CXXFLAGS) $(LDFLAGS) -shared -Wl,-soname,$(SONAME) -o $@ $^ $(LDLIBS)

# The program links the static library, so that it runs from the build directory.
$(PROGRAM): $(PROGRAM_OBJECTS) $(LIBRARY)
	$(CXX) $(ALL_CXXFLAGS) $(LDFLAGS) -o $@ $(PROGRAM_OBJECTS) $(LIBRARY) $(LDLIBS)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cc $(LIB_HEADERS) | $(OBJECT_DIRS)
	$(CXX) $(ALL_CXXFLAGS) -c -o $@ $<

# Order-only, so that touching a directory does not rebuild what is in it.
$(OBJECT_DIRS):
	mkdir -p $@

# Installing puts the library, its headers and a pkg-config file in place, for
# building other programs with it.  The x1 program is left out: the Python one
# is installed under that name.
prefix ?= /usr/local
libdir ?= $(prefix)/lib
includedir ?= $(prefix)/include
pkgconfigdir ?= $(libdir)/pkgconfig

install: lib
	install -d $(DESTDIR)$(libdir) $(DESTDIR)$(includedir)/girax1 $(DESTDIR)$(pkgconfigdir)
	install -m 0755 $(SHARED) $(DESTDIR)$(libdir)/
	ln -sf libgirax1-$(VERSION).so $(DESTDIR)$(libdir)/$(SONAME)
	ln -sf $(SONAME) $(DESTDIR)$(libdir)/libgirax1.so
	install -m 0644 $(LIBRARY) $(DESTDIR)$(libdir)/
	install -m 0644 $(LIB_HEADERS) $(DESTDIR)$(includedir)/girax1/
	sed -e 's|@PREFIX@|$(prefix)|' -e 's|@LIBDIR@|$(libdir)|' \
	    -e 's|@INCLUDEDIR@|$(includedir)|' -e 's|@VERSION@|$(VERSION)|' \
	    -e 's|@REQUIRES@|$(PKGS)|' $(SRC_DIR)/girax1.pc.in > $(DESTDIR)$(pkgconfigdir)/girax1.pc

# The RPM packages hold the Python x1 program and the C++ library.  They are
# built from the working tree: the spec file is generated from its template
# with the version girax1/__init__.py defines, so that the version is only kept
# in one place, and the sources are the files git tracks or would track.  Both
# are rebuilt when what they are made from changes; the index of git counts as
# well, since it changes when files are added or removed.  Inside rpmbuild the
# sources are no git working tree, which is why errors from git are dropped.
RPM_NAME = girax1
RPM_DIR = $(BUILD_DIR)/rpm
RPM_SPEC = $(RPM_DIR)/SPECS/$(RPM_NAME).spec
RPM_TARBALL = $(RPM_DIR)/SOURCES/$(RPM_NAME)-$(VERSION).tar.gz
RPM_FILES := $(wildcard $(shell git ls-files --cached --others --exclude-standard 2>/dev/null))

rpm: $(RPM_SPEC) $(RPM_TARBALL)
	rpmbuild -ba --define '_topdir $(abspath $(RPM_DIR))' $(RPM_SPEC)

$(RPM_SPEC): $(RPM_NAME).spec.in girax1/__init__.py | $(RPM_DIR)/SPECS
	test -n '$(VERSION)' || { echo 'no __version__ in girax1/__init__.py' >&2; exit 1; }
	sed 's/@VERSION@/$(VERSION)/g' $< > $@

$(RPM_TARBALL): $(RPM_FILES) $(wildcard .git/index) | $(RPM_DIR)/SOURCES
	tar czf $@ --transform 's,^,$(RPM_NAME)-$(VERSION)/,' $(RPM_FILES)

$(RPM_DIR)/SPECS $(RPM_DIR)/SOURCES:
	mkdir -p $@

clean:
	$(RM) -r $(BUILD_DIR)

.PHONY: all lib install clean rpm
