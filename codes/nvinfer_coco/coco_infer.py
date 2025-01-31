# coding: UTF-8
# Author: Yuri_Fanzhiwei
# 视频帧推理和推流部分模块

# 将文件夹内的包加入到路径下
import sys
sys.path.append('../')

# 设置gi库的版本为1.0
import gi
gi.require_version('Gst', '1.0')

import pyds
from gi.repository import GObject, Gst, GLib
from common.is_aarch_64 import is_aarch64
from common.bus_call import bus_call
from mqtt_module import mqtt_client
import multiprocessing as mp

CLASS_PERSON = 0

# 接收检测信息满足要求则利用MQTT发送警报到服务器
def handle_statistics(client, stats_queue, send_msg):
    while not stats_queue.empty():
        statistics = stats_queue.get_nowait()

        person_nums = int(statistics["People_nums"])
        alert = False
        if person_nums % 30==0 and person_nums != 0:
            alert = True

        if alert:
            alert = False
            print(person_nums)
            if client is not None:
                print('ALERT')
                mqtt_client.mqtt_publish(client, '/pub', send_msg)


# 定义需要检测的目标的类，并且创建类以类中属性作为整个程序的全局变量
class Person:
    def __init__(self, count):
        self.count = count


# MQTT模块和推理检测模块中间传递消息的模块，这里传递的消息就是以上述类
# 作为全局变量进行传递交换信息
# 使用GLib.timeout_add_seconds让该方法在程序的后台作为单独的子线程进
# 行运算，使得该方法每隔固定时间调用一次进行传递消息
def cb_add_statistics(cb_args):
    person, stats_queue = cb_args
    num = person.count
    if not stats_queue.full():
        stats_queue.put_nowait({"People_nums": num})

    # print("person num: ", num)
    GLib.timeout_add_seconds(1, cb_add_statistics, cb_args)



# 探针方法，插入pipeline中对视频帧进行目标检测，并将检测结果附到视频帧上去
def osd_sink_pad_buffer_probe(pad, info, cb_args):
    # 检测框数
    num_rects = 0
    # 视频帧数
    frame_number = 0
    # 获取目标检测类和异常队列
    person, e_ready = cb_args

    # 初始化检测需要检测目标的计数
    obj_counter = {
        CLASS_PERSON: 0
    }
    
    # 接收视频帧为一个buffer视频帧缓冲区
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # 从gst-buffer中接收一批次的数据包，其中由于gst-buffer当时是C语言编写所以
    # 在这里用python的时候需要获取gst-buffer中的数据的hash值
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # 将视频帧加入到pyds.NvDsFrameMeta类中进行处理
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break
        
        # 获取实时接收的视频帧数
        frame_number = frame_meta.frame_num
        # 获取实时检测的目标框数
        num_rects = frame_meta.num_obj_meta
        # 获取检测到的目标类
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                # 将目标类中信息假入到pyds.NvDsObjectMeta类中进行处理
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            
            # 检测到对应的目标进行计数
            obj_counter[obj_meta.class_id] += 1
            # ‘0’代表检测目标——人的类别ID，检测到就写入person目标类中的属性去
            if obj_meta.class_id == 0:
                person.count += 1
            # 遍历下一个检测到的目标
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # 获取显示数据并在左上角区域显示出检测到的信息

        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        py_nvosd_text_params = display_meta.text_params[0]
        py_nvosd_text_params.display_text = "Frame Number={} Person_count={}".format(frame_number, obj_counter[CLASS_PERSON])

        # 设置显示信息出现的位置坐标，左上角是坐标轴原点，设置offset偏移量
        py_nvosd_text_params.x_offset = 10
        py_nvosd_text_params.y_offset = 12
        # 设置字体大小，颜色
        py_nvosd_text_params.font_params.font_name = "Serif"
        py_nvosd_text_params.font_params.font_size = 10
        py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
        # 设置文本具有的背景颜色，如果指定则保存文本的背景颜色
        py_nvosd_text_params.set_bg_clr = 1
        py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)
        # 终端打印显示的文本信息
        print(pyds.get_string(py_nvosd_text_params.display_text))
        # 视频帧上显示文本信息
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

        # 进入下一帧进行检测
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def infer_main(args, stats_queue: mp.Queue = None, e_ready: mp.Event = None):
    # 初始化目标类
    person = Person(0)
    
    # 如果输入参数错误报错
    if len(args) != 2:
        sys.stderr.write("usage: %s <v4l2-device-path>\n" % args[0])
        sys.exit(1)

    # 初始化GStreamer
    GObject.threads_init()
    Gst.init(None)

    # 创建GStreamer的管道
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")

    # 添加CSI摄像头设备作为视频源输并设置输入摄像头设备的设备号
    print("Creating Source \n ")
    source = Gst.ElementFactory.make("nvarguscamerasrc", "usb-cam-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")
    source.set_property("sensor-id", int(args[1]))
    source.set_property("bufapi-version", 1)

    # 创建摄像头滤波器，主要是为了传递视频数据
    caps_v4l2src = Gst.ElementFactory.make("capsfilter", "v4l2src_caps")
    if not caps_v4l2src:
        sys.stderr.write(" Unable to create capsfilter \n")

    # Adding videoconvert -> nvvideoconvert as not all
    # raw formats are supported by nvvideoconvert;
    # Say YUYV is unsupported - which is the common
    # raw format for many logi usb cams
    # In case we have a camera with raw format supported in
    # nvvideoconvert, GStreamer plugins' capability negotiation
    # shall be intelligent enough to reduce compute by
    # videoconvert doing passthrough (TODO we need to confirm this)
    # videoconvert to make sure a superset of raw formats are supported

    # 创建视频数据格式转换器，将视频从一个色彩空间转换到另一个色彩空间
    print("Creating Video Converter \n")
    vidconvsrc = Gst.ElementFactory.make("videoconvert", "convertor_src1")
    if not vidconvsrc:
        sys.stderr.write(" Unable to create videoconvert \n")

    nvvidconvsrc = Gst.ElementFactory.make("nvvidconv", "convertor_src2")
    if not nvvidconvsrc:
        sys.stderr.write(" Unable to create Nvvideoconvert \n")

    caps_vidconvsrc = Gst.ElementFactory.make("capsfilter", "nvmm_caps")
    if not caps_vidconvsrc:
        sys.stderr.write(" Unable to create capsfilter \n")

    # 创建英伟达的视频流接收处理，可以从一个source或者多个source得到一批次的视频帧
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    # 使用英伟达nvinfer来加速推理模型从而输出结果
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")

    # nvosd需要RGBA格式的数据，因此将数据格式从NV12转换到RGBA
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")

    # 创建英伟达的OSD插件来实现显示输出上的目标框取
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")

    # 创建传输视频流的队列
    queue0 = Gst.ElementFactory.make("queue")

    # 创建英伟达的视频转换器
    nvvidconv_postosd = Gst.ElementFactory.make("nvvideoconvert", "convertor_postosd")

    # 创建摄像头滤波器，主要是为了传递视频数据
    caps = Gst.ElementFactory.make("capsfilter", "filter")
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420"))

    # 创建英伟达的硬件编码器，使用H264编码
    encoder = Gst.ElementFactory.make("nvv4l2h264enc", "encoder")
    encoder.set_property("maxperf-enable", 1)
    encoder.set_property("preset-level", 1)
    encoder.set_property("profile", 4)
    # encoder.set_property("bufapi-version", 1)
    # encoder.set_property("insert-sps-pps", 1)
    encoder.set_property("iframeinterval", 500)
    encoder.set_property("control-rate", 1)
    encoder.set_property("bitrate", 2000000)

    # 创建传输视频流的队列
    queue = Gst.ElementFactory.make("queue")

    # 创建视频流的H264编码参数
    h264parse = Gst.ElementFactory.make("h264parse")

    # 创建FLV视频流格式为了RTMP推流
    flvmux = Gst.ElementFactory.make("flvmux")
    flvmux.set_property("streamable", True)

    if is_aarch64():
        transform = Gst.ElementFactory.make("nvegltransform", "nvegl-transform")

    # 创建RTMP的接收器，准备通过RTMP把视频流推到流媒体服务器
    print("Creating RTMP-Sink \n")
    sink = Gst.ElementFactory.make("rtmpsink")
    sink.set_property("location", "rtmp://59.110.7.232:1935/rtmplive")
    if not sink:
        sys.stderr.write(" Unable to create egl sink \n")

    # 摄像头开始运行，并设置其图像宽度和高度以及采集的视频帧率
    print("Playing CAM %s " % args[1])
    caps_v4l2src.set_property('caps', Gst.Caps.from_string("video/x-raw(memory:NVMM), framerate=30/1"))
    caps_vidconvsrc.set_property('caps', Gst.Caps.from_string("video/x-raw(memory:NVMM)"))
    # source.set_property('device', args[1])
    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batch-size', 1)
    streammux.set_property('batched-push-timeout', 4000000)

    # 设置检测算法配置文件的路径并载入
    pgie.set_property('config-file-path', "config_infer_primary_yoloV3_tiny.txt")
    # Set sync = false to avoid late frame drops at the display-sink
    # sink.set_property('sync', False)

    # 将所有的元件加入到管道中去
    print("Adding elements to Pipeline \n")
    pipeline.add(source)
    pipeline.add(caps_v4l2src)
    pipeline.add(vidconvsrc)
    pipeline.add(nvvidconvsrc)
    pipeline.add(caps_vidconvsrc)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    pipeline.add(queue0)
    pipeline.add(nvvidconv_postosd)
    pipeline.add(caps)
    pipeline.add(encoder)
    pipeline.add(queue)
    pipeline.add(h264parse)
    pipeline.add(flvmux)
    pipeline.add(sink)
    if is_aarch64():
        pass
        # pipeline.add(transform)

    # 连接管道中的各个元件
    # v4l2src -> nvvideoconvert -> mux -> nvinfer -> nvvideoconvert -> nvosd ->
    # queue -> nvvideoconvert -> caps -> encoder -> queue -> h264parse -> flvmux -> rtmpsink
    source.link(caps_v4l2src)
    caps_v4l2src.link(vidconvsrc)
    vidconvsrc.link(nvvidconvsrc)
    nvvidconvsrc.link(caps_vidconvsrc)

    sinkpad = streammux.get_request_pad("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")
    srcpad = caps_vidconvsrc.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of caps_vidconvsrc \n")

    srcpad.link(sinkpad)
    streammux.link(pgie)
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    if is_aarch64():
        nvosd.link(queue0)
        queue0.link(nvvidconv_postosd)
        # transform.link(sink)
    else:
        nvosd.link(queue0)
        queue0.link(nvvidconv_postosd)

    nvvidconv_postosd.link(caps)
    caps.link(encoder)
    encoder.link(queue)
    queue.link(h264parse)
    h264parse.link(flvmux)
    flvmux.link(sink)

    # 加入检测探针方法
    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")
    cb_args = (person, e_ready)
    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe, cb_args)


    # 创建GStreamer的事件循环从而不断的获取bus总线上的信息
    loop = GObject.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)
    print("Starting pipeline \n")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        # 实时传递检测的信息
        cb_args = person, stats_queue
        GLib.timeout_add_seconds(1, cb_add_statistics, cb_args)
        loop.run()
    except:
        pass
    pipeline.set_state(Gst.State.NULL)


if __name__ == '__main__':
    send_msg = {'path': '/2021', 'device': 'nano'}
    ip = "59.110.7.232"
    port = 1883
    client = mqtt_client.mqtt_init(ip, port)
    client = mqtt_client.mqtt_subscribe(client, '/sub')
    client.on_message = mqtt_client.mqtt_get_message

    stats_queue = mp.Queue(maxsize=5)
    main_process = mp.Process(target=main, args=(sys.argv, stats_queue))
    main_process.start()
    # main(sys.argv, stats_queue)
    while True:
        handle_statistics(client, stats_queue, send_msg)
