#[cfg(windows)]
#[path = "../src/backend_job.rs"]
mod backend_job;

#[cfg(windows)]
#[test]
fn dropping_job_terminates_the_assigned_backend_process() {
    use std::os::windows::process::CommandExt;
    use std::process::Command;
    use std::thread;
    use std::time::{Duration, Instant};

    let mut child = Command::new("cmd.exe")
        .args(["/C", "ping 127.0.0.1 -n 30 >nul"])
        .creation_flags(0x08000000)
        .spawn()
        .expect("test child should start");
    let job = backend_job::KillOnCloseJob::assign(&child)
        .expect("test child should join the kill-on-close job");
    assert!(child
        .try_wait()
        .expect("child status should be readable")
        .is_none());

    drop(job);
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if child
            .try_wait()
            .expect("child status should be readable")
            .is_some()
        {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }

    let _ = child.kill();
    panic!("closing the job did not terminate the assigned child");
}
