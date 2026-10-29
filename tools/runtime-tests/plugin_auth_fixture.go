//go:build linux && native_backend && plugin_auth_test

package drm

/*
typedef void (*drm_auth_callback_t)(const char *challenge_type, char *output_buffer, int buffer_size, void *user_data);
typedef void (*drm_state_callback_t)(const char *state_name, void *user_data);

typedef struct {
    const char              *base_directory;
    const char              *lib64_directory;
    const char              *username;
    const char              *password;
    const char              *device_info;
    int                      offline_only;
    drm_auth_callback_t      auth_callback;
    void                    *auth_user_data;
    drm_state_callback_t     state_callback;
    void                    *state_user_data;
} drm_config;


int drm_init(const drm_config *config);
void drm_shutdown(void);

#include <stdlib.h>
extern void nativeBridgeAuth(char *, char *, int, void *);
extern void nativeBridgeState(char *, void *);
extern void drm_forward_auth(const char *, char *, int, void *);
*/
import "C"
import (
	"context"
	"runtime/cgo"
	"unsafe"
)

// exerciseNativeAuth runs the production registration and C forwarding bridge.
// The fixture never loads Apple's libraries or contacts an account service.
func exerciseNativeAuth(ctx context.Context, source AuthSource, base string, size int) (string, []string) {
	auth := cgo.NewHandle(nativeAuthContext{ctx, source})
	defer auth.Delete()
	states := make(chan string, 16)
	state := cgo.NewHandle(states)
	defer state.Delete()
	path := C.CString(base)
	defer C.free(unsafe.Pointer(path))
	cfg := C.drm_config{
		base_directory:  path,
		auth_callback:   (C.drm_auth_callback_t)(C.nativeBridgeAuth),
		auth_user_data:  unsafe.Pointer(uintptr(auth)),
		state_callback:  (C.drm_state_callback_t)(C.nativeBridgeState),
		state_user_data: unsafe.Pointer(uintptr(state)),
	}
	C.drm_init(&cfg)
	defer C.drm_shutdown()
	code := C.CString("2fa")
	defer C.free(unsafe.Pointer(code))
	out := C.calloc(C.size_t(size), 1)
	defer C.free(out)
	C.drm_forward_auth(code, (*C.char)(out), C.int(size), nil)
	var got []string
	for len(states) > 0 {
		got = append(got, <-states)
	}
	return C.GoString((*C.char)(out)), got
}
