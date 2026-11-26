//! The few POSIX calls lit needs that the standard library does not offer.

#[cfg(unix)]
mod unix {
    use std::sync::atomic::{AtomicI32, Ordering};

    extern "C" {
        fn geteuid() -> u32;
        fn kill(pid: i32, sig: i32) -> i32;
        fn signal(sig: i32, handler: usize) -> usize;
    }

    pub const SIGINT: i32 = 2;
    pub const SIGTERM: i32 = 15;

    pub fn euid() -> u32 {
        // SAFETY: geteuid takes no arguments and cannot fail.
        unsafe { geteuid() }
    }

    static CHILD: AtomicI32 = AtomicI32::new(0);

    extern "C" fn forward(sig: i32) {
        let pid = CHILD.load(Ordering::SeqCst);
        if pid > 0 {
            // SAFETY: kill is async-signal-safe.
            unsafe { kill(pid, sig) };
        }
    }

    extern "C" fn ignore(_: i32) {}

    /// While the child runs, Ctrl-C reaches the whole process group, so the child
    /// sees it on its own and lit only keeps running to wait for it; a SIGTERM sent
    /// to lit alone is passed on to the child.
    pub fn forward_signals_to(pid: u32) {
        CHILD.store(pid as i32, Ordering::SeqCst);
        // SAFETY: both handlers only touch an atomic and call kill.
        unsafe {
            signal(SIGINT, ignore as extern "C" fn(i32) as usize);
            signal(SIGTERM, forward as extern "C" fn(i32) as usize);
        }
    }
}

#[cfg(unix)]
pub use unix::*;
