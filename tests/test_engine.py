import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import json
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import GPU, Runner, graphics_verdict, memory_verdict, write_html, overlay_environment, HUD_LIBRARY, camera_devices, camera_mode, CameraPreview


class FakeGPU:
    vendor='AMD'
    info={'model':'Fake <GPU>','vendor':'AMD','pci_address':'0000:04:00.0','vram':'8 GiB','manufacturer_serial':None}
    def sample(self):
        return {'timestamp':'now','core_c':40,'junction_c':50,'memory_c':50,
                'gpu_load_pct':99,'vram_used_mib':1000,'power_w':80,'fan':1000}


class VerdictTests(unittest.TestCase):
    def test_overlay_preload_is_scoped_and_preserves_other_libraries(self):
        env={'LD_PRELOAD':'existing.so','OTHER':'value'}
        with patch('engine.Path.is_file', return_value=True):
            enabled=overlay_environment(env)
        self.assertEqual(env,{'LD_PRELOAD':'existing.so','OTHER':'value'})
        self.assertEqual(enabled['LD_PRELOAD'],str(HUD_LIBRARY)+':existing.so')
        self.assertEqual(enabled['MANGOHUD'],'1')
        self.assertIn('fps_limit=0',enabled['MANGOHUD_CONFIG'])
        self.assertIn('gpu_temp',enabled['MANGOHUD_CONFIG'])
        self.assertIn('vram',enabled['MANGOHUD_CONFIG'])
        self.assertEqual(overlay_environment(env,False),env)

    def test_nvidia_identity_and_unavailable_optional_sensors(self):
        xml='<nvidia_smi_log><driver_version>610.57.04</driver_version><gpu><product_name>NVIDIA RTX 3060</product_name><uuid>GPU-123</uuid><serial>N/A</serial><vbios_version>1.0</vbios_version><fb_memory_usage><total>12288 MiB</total></fb_memory_usage></gpu></nvidia_smi_log>'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'0000:02:00.0';path.mkdir()
            (path/'vendor').write_text('0x10de')
            def fake(args,**kwargs):
                if args[0]=='lspci': return 'NVIDIA VGA controller'
                if '-q' in args: return xml
                return '55, 98, 2000, 105.5, N/A'
            with patch('engine.command',side_effect=fake):
                gpu=GPU(path)
                self.assertEqual(gpu.vendor,'NVIDIA')
                self.assertEqual(gpu.info['uuid'],'GPU-123')
                self.assertIsNone(gpu.info['manufacturer_serial'])
                sample=gpu.sample()
                self.assertEqual(sample['core_c'],55)
                self.assertIsNone(sample['fan'])
                self.assertEqual(sample['power_w'],105.5)

    def test_amd_missing_core_sensor_rejects_stress_test(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            (path/'vendor').write_text('0x1002')
            with patch('engine.command',return_value='AMD VGA'):
                gpu=GPU(path)
                with self.assertRaisesRegex(RuntimeError,'temperature sensors unavailable'):
                    gpu.sample()

    def test_memory_needs_final_pass_and_five_minutes(self):
        result={'elapsed_seconds':305,'stop_reason':'stage duration'}
        log='Standard 5-minute test PASSed!'
        self.assertEqual(memory_verdict(log,result),'INCOMPLETE')
        log+='\nmemtest_vulkan: no any errors, testing PASSed.'
        self.assertEqual(memory_verdict(log,result),'PASS')
        result['elapsed_seconds']=20
        self.assertEqual(memory_verdict(log,result),'INCOMPLETE')

    def test_cancel_and_thermal_stop_cannot_pass(self):
        for reason in ['cancelled','temperature limit','monitoring failure','session deadline','recording failure','webcam failure']:
            with self.subTest(reason=reason):
                self.assertEqual(memory_verdict('no any errors, testing PASSed',{'elapsed_seconds':305,'stop_reason':reason}),'INCOMPLETE')

    def test_memory_error_overrides_pass(self):
        self.assertEqual(memory_verdict('Error found\nno any errors, testing PASSed',{'elapsed_seconds':305,'stop_reason':'stage duration'}),'FAIL')

    def test_graphics_rejects_invalid_and_software_renderer(self):
        result={'return_code':0,'stop_reason':None}
        good='OpenGL renderer: AMD Radeon RX 6600\nPhoronix finished\nFPS: 91.05'
        self.assertEqual(graphics_verdict(good,result,FakeGPU()),('PASS',91.05))
        self.assertEqual(graphics_verdict(good+'\nOpenGL error: invalid framebuffer operation',result,FakeGPU())[0],'TOOL ERROR')
        self.assertEqual(graphics_verdict(good.replace('AMD Radeon RX 6600','llvmpipe'),result,FakeGPU())[0],'TOOL ERROR')
        self.assertEqual(graphics_verdict(good,{'return_code':0,'stop_reason':'session deadline'},FakeGPU())[0],'INCOMPLETE')
        nvidia=FakeGPU(); nvidia.vendor='NVIDIA'
        self.assertEqual(graphics_verdict(good,result,nvidia)[0],'TOOL ERROR')
        self.assertEqual(graphics_verdict(good.replace('AMD Radeon RX 6600','NVIDIA GeForce RTX 3060'),result,nvidia)[0],'PASS')


class ProcessTests(unittest.TestCase):
    def test_camera_loss_stops_test_and_cannot_pass(self):
        class LostCamera:
            def alive(self): return False
        with tempfile.TemporaryDirectory() as folder:
            runner,_=self.setup_runner(folder)
            runner.webcam=LostCamera()
            result=runner.stage('memory',[sys.executable,'-c','import time;time.sleep(20)'],folder,10,signal.SIGINT)
            self.assertEqual(result['stop_reason'],'webcam failure')
            self.assertEqual(result['status'],'INCOMPLETE')
            self.assertLess(result['elapsed_seconds'],2)

    def test_total_budget_cannot_be_increased_above_cap(self):
        runner=Runner(FakeGPU(),'/tmp',lambda *_:None,memory_seconds=10000,session_seconds=10000)
        self.assertEqual(runner.memory_seconds,305)
        self.assertEqual(runner.session_seconds,590)

    def setup_runner(self,folder,gpu=None):
        events=[]
        runner=Runner(gpu or FakeGPU(),folder,lambda k,v:events.append((k,v)))
        runner.raw=Path(folder)
        runner.env=os.environ.copy()
        runner.started=time.monotonic()
        runner.deadline=runner.started+10
        return runner,events

    def test_stage_duration_stops_and_drains_final_output(self):
        with tempfile.TemporaryDirectory() as folder:
            runner,events=self.setup_runner(folder)
            script='import signal,time,sys\ndef end(*args):\n print("FINAL OUTPUT",flush=True); sys.exit(0)\nsignal.signal(signal.SIGINT,end)\nprint("LIVE OUTPUT",flush=True)\ntime.sleep(20)'
            result=runner.stage('memory',[sys.executable,'-c',script],folder,.2,signal.SIGINT)
            text=(Path(folder)/'memory.log').read_text()
            self.assertIn('LIVE OUTPUT',text)
            self.assertIn('FINAL OUTPUT',text)
            self.assertEqual(result['stop_reason'],'stage duration')
            self.assertLess(result['elapsed_seconds'],2)
            self.assertTrue(any(k=='sample' for k,v in events))

    def test_temperature_limit_stops_before_timeout(self):
        class HotGPU(FakeGPU):
            def sample(self):
                row=super().sample();row['core_c']=86;return row
        with tempfile.TemporaryDirectory() as folder:
            runner,_=self.setup_runner(folder,HotGPU())
            result=runner.stage('memory',[sys.executable,'-c','import time;time.sleep(20)'],folder,10,signal.SIGINT)
            self.assertEqual(result['stop_reason'],'temperature limit')
            self.assertEqual(result['status'],'INCOMPLETE')
            self.assertLess(result['elapsed_seconds'],2)

    def test_monitoring_failure_stops(self):
        class BrokenGPU(FakeGPU):
            def sample(self): raise RuntimeError('sensor missing')
        with tempfile.TemporaryDirectory() as folder:
            runner,_=self.setup_runner(folder,BrokenGPU())
            result=runner.stage('memory',[sys.executable,'-c','import time;time.sleep(20)'],folder,10,signal.SIGINT)
            self.assertEqual(result['stop_reason'],'monitoring failure')

    def test_cancel_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            runner,_=self.setup_runner(folder)
            timer=threading.Timer(.2,runner.cancel.set);timer.start()
            result=runner.stage('memory',[sys.executable,'-c','import time;time.sleep(20)'],folder,10,signal.SIGINT)
            timer.join()
            self.assertEqual(result['stop_reason'],'cancelled')
            self.assertLess(result['elapsed_seconds'],2)

    def test_watchdog_interrupts_owned_process(self):
        with tempfile.TemporaryDirectory() as folder:
            runner,_=self.setup_runner(folder)
            proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],start_new_session=True)
            runner.process=proc
            runner.finished=threading.Event()
            runner.deadline=time.monotonic()+.2
            runner.watchdog()
            proc.wait(timeout=2)
            self.assertTrue(runner.cancel.is_set())
            self.assertNotEqual(proc.returncode,0)

    def test_lock_prevents_simultaneous_tests(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'XDG_RUNTIME_DIR':folder}):
            one,_=self.setup_runner(folder);two,_=self.setup_runner(folder)
            one.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError,'Another GPU Tester'):
                    two.acquire()
            finally: one.lock.close()


class ExportTests(unittest.TestCase):
    def test_reports_disabled_still_saves_json_logs_and_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            dest=Path(folder)
            runner=Runner(FakeGPU(),dest,lambda *_:None,reports=False)
            runner.dest=dest
            runner.raw=dest/'raw';runner.raw.mkdir()
            (runner.raw/'memory.log').write_text('partial test')
            journal=subprocess.CompletedProcess([],0,stdout='',stderr='')
            with patch('engine.subprocess.run',return_value=journal):
                runner.finalize()
            self.assertTrue((dest/'results.json').exists())
            self.assertFalse((dest/'GPU-test-report.html').exists())
            self.assertFalse((dest/'GPU-test-report.pdf').exists())
            with zipfile.ZipFile(dest/'GPU-evidence.zip') as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('raw/memory.log',archive.namelist())
                self.assertIn('SHA256SUMS',archive.namelist())
                self.assertEqual(json.loads(archive.read('results.json'))['status'],'INCOMPLETE')

    def test_partial_report_escapes_identity_and_discloses_no_score(self):
        with tempfile.TemporaryDirectory() as folder:
            runner,_=ProcessTests().setup_runner(folder)
            runner.result['error']='Driver <error>'
            write_html(Path(folder),runner.result)
            text=(Path(folder)/'GPU-test-report.html').read_text()
            self.assertIn('Fake &lt;GPU&gt;',text)
            self.assertIn('Driver &lt;error&gt;',text)
            self.assertIn('INCOMPLETE',text)
            self.assertIn('Manufacturer serial: Unavailable',text)


class WebcamTests(unittest.TestCase):
    def test_enumeration_rejects_metadata_node(self):
        def fake(args,**kwargs):
            if '--list-devices' in args: return 'UVC Camera:\n\t/dev/video0\n\t/dev/video1\n'
            return ('Capabilities: Video Capture\nDevice Caps : 0x1\n\tVideo Capture\n'
                    if '/dev/video0' in args else 'Capabilities: Video Capture\nDevice Caps : 0x2\n\tMetadata Capture\n')
        with patch('engine.shutil.which',return_value='/usr/bin/v4l2-ctl'),patch('engine.command',side_effect=fake):
            self.assertEqual(camera_devices(),[{'device':'/dev/video0','name':'UVC Camera'}])

    def test_camera_mode_prefers_supported_30fps_mjpeg(self):
        text="""[0]: 'MJPG' (Motion-JPEG)
        Size: Discrete 640x480
        Interval: Discrete 0.033s (30.000 fps)
        Interval: Discrete 0.067s (15.000 fps)
        [1]: 'YUYV'
        Size: Discrete 1280x960
        Interval: Discrete 0.133s (7.500 fps)"""
        with patch('engine.command',return_value=text):
            self.assertEqual(camera_mode('/dev/video0'),{'width':640,'height':480,'fps':30.0,'pixel':'MJPG'})

    def test_camera_paths_are_v4l2_only(self):
        with self.assertRaises(ValueError): CameraPreview('https://camera.example/')

    def test_camera_close_only_stops_owned_process(self):
        from unittest.mock import Mock
        camera=CameraPreview('/dev/video0')
        proc=Mock();camera.process=proc
        with patch('engine.stop_process') as stop:
            camera.close()
            stop.assert_called_once_with(proc,timeout=1)
        self.assertIsNone(camera.process)


if __name__=='__main__': unittest.main()
