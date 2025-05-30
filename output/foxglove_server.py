import asyncio
import json
import logging
import time
from base64 import b64encode
from typing import Set, Type

import cv2
import google.protobuf.message
from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
from foxglove_schemas_protobuf.FrameTransform_pb2 import FrameTransform
from foxglove_schemas_protobuf.ImageAnnotations_pb2 import ImageAnnotations
from foxglove_schemas_protobuf.Log_pb2 import Log
from foxglove_schemas_protobuf.SceneUpdate_pb2 import SceneUpdate
from foxglove_websocket.server import FoxgloveServer, FoxgloveServerListener
from foxglove_websocket.types import ChannelId
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet

import output.pipeline
import util.state as state
from output.float_message_pb2 import FloatMessage
from output.foxglove_image import get_frame, get_image
from output.foxglove_pose import get_field, get_pose
from output.foxglove_utils import run_cancellable, timestamp
from util.state import logger, settings


def build_file_descriptor_set(
    message_class: Type[google.protobuf.message.Message],
) -> FileDescriptorSet:
    """
    Build a FileDescriptorSet representing the message class and its dependencies.
    """
    file_descriptor_set = FileDescriptorSet()
    seen_dependencies: Set[str] = set()

    def append_file_descriptor(file_descriptor: FileDescriptor):
        for dep in file_descriptor.dependencies:
            if dep.name not in seen_dependencies:
                seen_dependencies.add(dep.name)
                append_file_descriptor(dep)
        file_descriptor.CopyToProto(file_descriptor_set.file.add())  # type: ignore

    append_file_descriptor(message_class.DESCRIPTOR.file)
    return file_descriptor_set


log: Log = None


class FoxgloveWSHandler(logging.Handler):
    def __init__(self, server: FoxgloveServer):
        super().__init__()
        self.server = server

    @staticmethod
    def record_to_log(record: logging.LogRecord):
        return Log(
            timestamp=timestamp(int(record.created * 1e9)),
            level=record.levelname,
            message=record.getMessage(),
        )

    def emit(self, record):
        global log
        try:
            log_entry = self.record_to_log(record)
            log = log_entry

        except Exception:
            # Handle any errors that occur during logging
            self.handleError(record)


field_reset = False
config_reset = False


async def main():
    global field_reset, config_reset, log

    class Listener(FoxgloveServerListener):
        async def on_subscribe(self, server: FoxgloveServer, channel_id: ChannelId):
            global field_reset, config_reset
            print("First client subscribed to", channel_id)
            if str(channel_id) == "7":
                field_reset = True
            elif str(channel_id) == "8":
                config_reset = True

        async def on_unsubscribe(self, server: FoxgloveServer, channel_id: ChannelId):
            print("Last client unsubscribed from", channel_id)

    async with FoxgloveServer("0.0.0.0", 8765, "Iris", logger=logger) as server:
        server.set_listener(Listener())
        ambiguity_pose_pub = await server.add_channel(
            {
                "topic": "/ambiguity/pose",
                "encoding": "protobuf",
                "schemaName": FrameTransform.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(FrameTransform).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        pose_pub = await server.add_channel(
            {
                "topic": "/camera/pose",
                "encoding": "protobuf",
                "schemaName": FrameTransform.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(FrameTransform).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        annotations_pub = await server.add_channel(
            {
                "topic": "/camera/annotations",
                "encoding": "protobuf",
                "schemaName": ImageAnnotations.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(ImageAnnotations).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        ignored_annotations_pub = await server.add_channel(
            {
                "topic": "/camera/ignored_annotations",
                "encoding": "protobuf",
                "schemaName": ImageAnnotations.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(ImageAnnotations).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        fps_pub = await server.add_channel(
            {
                "topic": "/camera/fps",
                "encoding": "protobuf",
                "schemaName": FloatMessage.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(FloatMessage).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        image_pub = await server.add_channel(
            {
                "topic": "/camera/image",
                "encoding": "protobuf",
                "schemaName": CompressedImage.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(CompressedImage).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        field_pub = await server.add_channel(
            {
                "topic": "/world/field",
                "encoding": "protobuf",
                "schemaName": SceneUpdate.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(SceneUpdate).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )
        config_pub = await server.add_channel(
            {
                "topic": "/config",
                "encoding": "json",
                "schemaName": "Configuration",
                "schema": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "msg": {"type": "string"},
                            "count": {"type": "number"},
                        },
                    }
                ),
                "schemaEncoding": "jsonschema",
            }
        )
        log_pub = await server.add_channel(
            {
                "topic": "/log",
                "encoding": "protobuf",
                "schemaName": Log.DESCRIPTOR.full_name,
                "schema": b64encode(
                    build_file_descriptor_set(Log).SerializeToString()
                ).decode("ascii"),
                "schemaEncoding": "protobuf",
            }
        )

        ws_handler = FoxgloveWSHandler(server)
        ws_handler.setLevel(logging.DEBUG)
        logger.addHandler(ws_handler)

        while True:
            try:
                await asyncio.sleep(0.05)
                now = time.time_ns()

                frame, scale = output.pipeline.process_image(
                    settings.foxglove_server.max_res
                )
                (
                    points,
                    ids,
                    ignored_points,
                    ignored_ids,
                ) = output.pipeline.process_detections(scale)
                if frame is None:
                    continue
                else:
                    # Encode the frame in JPEG format
                    encode_param = [
                        int(cv2.IMWRITE_JPEG_QUALITY),
                        settings.foxglove_server.quality,
                    ]
                    ret, buffer = cv2.imencode(".jpg", frame, encode_param)
                    if not ret:
                        continue

                # Convert the frame to bytes
                data = buffer.tobytes()
                img = get_image(now, data)
                ann, ignored_ann, fps = get_frame(
                    now, points, ids, ignored_points, ignored_ids
                )
                if len(state.poses) > 0:
                    pose = get_pose(now, state.poses[0], "camera")
                    await server.send_message(pose_pub, now, pose.SerializeToString())
                if len(state.poses) > 1:
                    ambiguity = get_pose(now, state.poses[1], "ambiguity")
                    await server.send_message(
                        ambiguity_pose_pub, now, ambiguity.SerializeToString()
                    )

                if field_reset:
                    await server.send_message(
                        field_pub, now, get_field(now).SerializeToString()
                    )
                    field_reset = False
                if config_reset:
                    await server.send_message(
                        config_pub, now, state.config_json.encode("utf8")
                    )
                await server.send_message(image_pub, now, img.SerializeToString())
                await server.send_message(annotations_pub, now, ann.SerializeToString())
                await server.send_message(
                    ignored_annotations_pub, now, ignored_ann.SerializeToString()
                )
                await server.send_message(fps_pub, now, fps.SerializeToString())
                if log is not None:
                    await server.send_message(log_pub, now, log.SerializeToString())
                    log = None

            except Exception as e:
                logger.exception(e)


def start():
    run_cancellable(main())


if __name__ == "__main__":
    run_cancellable(main())
