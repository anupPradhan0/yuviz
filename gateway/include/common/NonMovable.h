#pragma once

namespace voiceai {

// Inherit privately to make a class non-movable.
// Typically combined with NonCopyable for types that own non-transferable resources.
//
//   class Foo : private NonCopyable, private NonMovable { ... };
//
class NonMovable {
protected:
    NonMovable()  = default;
    ~NonMovable() = default;

    NonMovable(NonMovable&&)            = delete;
    NonMovable& operator=(NonMovable&&) = delete;
};

} // namespace voiceai
