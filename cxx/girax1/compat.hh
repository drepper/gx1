#pragma once

// What the library needs from the standard library, and what it does without.
//
// The sources are written for C++26 but nothing in them depends on it: built as
// C++23 they only lose the reason on a deleted function, which is a diagnostic
// nicety rather than behaviour.  The Makefile picks the newest standard the
// compiler accepts, so this adapts on its own.

#include <version>

// Everything below is C++23 and is genuinely required; saying so here beats a
// page of errors from the first header that misses one.
#if !defined(__cpp_lib_expected)
#  error "girax1 needs std::expected (C++23; libstdc++ 12, libc++ 16)"
#endif
#if !defined(__cpp_lib_generator)
#  error "girax1 needs std::generator (C++23; libstdc++ 14)"
#endif
#if !defined(__cpp_lib_format)
#  error "girax1 needs std::format (C++20 as published in C++23; libstdc++ 13)"
#endif

// A deleted function may carry the reason it is deleted since C++26.  Where it
// may not, the deletion still stands and only the explanation is dropped.
#if defined(__cpp_deleted_function) && __cpp_deleted_function >= 202403L
#  define GIRAX1_DELETED(why) delete (why)
#else
#  define GIRAX1_DELETED(why) delete
#endif
