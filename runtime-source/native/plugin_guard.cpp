// Catch vendor exceptions inside the Android unwinder's loaded-image set.
// No host C++ objects or exception payloads cross this ABI boundary.
class SVError {
public:
    virtual ~SVError();
    unsigned char errorCode() const;
    int componentErrorCode() const;
};
using Call = void (*)(void*,void*,void*,void*,void*,void*,void*,void*,void*,void*);
extern "C" int plugin_guard_call(Call call, void **args, int *code, int *component) {
    try {
        call(args[0],args[1],args[2],args[3],args[4],args[5],args[6],args[7],args[8],args[9]);
        return 0;
    } catch (const SVError &error) {
        *code = error.errorCode();
        *component = error.componentErrorCode();
        return -1;
    } catch (...) {
        *code = -1;
        *component = 0;
        return -1;
    }
}
