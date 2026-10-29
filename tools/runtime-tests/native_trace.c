/* Diagnostic-only signal trace for a credential-free disposable probe. */
#define _GNU_SOURCE
#include <execinfo.h>
#include <signal.h>
#include <unistd.h>
static void crash(int signal_number)
{
    void *frames[24];
    int count = backtrace(frames, 24);
    backtrace_symbols_fd(frames, count, STDERR_FILENO);
    _exit(128 + signal_number);
}
__attribute__((constructor)) static void install_trace(void)
{
    struct sigaction action = {0};
    action.sa_handler = crash;
    sigemptyset(&action.sa_mask);
    sigaction(SIGSEGV, &action, 0);
    sigaction(SIGABRT, &action, 0);
}
