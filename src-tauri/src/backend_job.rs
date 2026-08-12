use std::process::Child;

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

#[cfg(windows)]
pub struct KillOnCloseJob(usize);

#[cfg(not(windows))]
pub struct KillOnCloseJob;

#[cfg(windows)]
impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0 as HANDLE);
        }
    }
}

#[cfg(windows)]
impl KillOnCloseJob {
    pub fn assign(child: &Child) -> Result<Self, String> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(format!(
                    "无法创建后端进程保护作业：{}",
                    std::io::Error::last_os_error()
                ));
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const std::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if configured == 0 {
                let error = std::io::Error::last_os_error();
                let _ = CloseHandle(job);
                return Err(format!("无法配置后端进程保护作业：{error}"));
            }

            let process_handle = child.as_raw_handle() as HANDLE;
            if AssignProcessToJobObject(job, process_handle) == 0 {
                let error = std::io::Error::last_os_error();
                let _ = CloseHandle(job);
                return Err(format!("无法把后端加入进程保护作业：{error}"));
            }
            Ok(Self(job as usize))
        }
    }
}

#[cfg(not(windows))]
impl KillOnCloseJob {
    pub fn assign(_child: &Child) -> Result<Self, String> {
        Ok(Self)
    }
}
