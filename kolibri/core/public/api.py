import gzip
import io
import json
import time

from django.db.models import Q
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseNotFound
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.gzip import gzip_page
from morango.models.core import TransferSession
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .. import error_constants
from .utils import get_device_info
from kolibri.core.auth.models import Facility
from kolibri.core.content.models import ChannelMetadata
from kolibri.core.content.models import ContentNode
from kolibri.core.content.models import LocalFile
from kolibri.core.content.serializers import PublicChannelSerializer
from kolibri.core.content.utils.file_availability import generate_checksum_integer_mask
from kolibri.core.device.models import SyncQueue
from kolibri.core.device.utils import allow_peer_unlisted_channel_import

MAX_CONCURRENT_SYNCS = 5
SYNC = "sync"  # can begin a sync right now
QUEUED = "queued"  # request added to the queue


class InfoViewSet(viewsets.ViewSet):
    """
    An equivalent endpoint in studio which allows kolibri devices to know
    if this device can serve content.
    Spec doc: https://docs.google.com/document/d/1XKXQe25sf9Tht6uIXvqb3T40KeY3BLkkexcV08wvR9M/edit#
    """

    def list(self, request):
        """Returns metadata information about the device"""

        return Response(get_device_info())


def _get_channel_list(version, params, identifier=None):
    if version == "v1":
        return _get_channel_list_v1(params, identifier=identifier)
    else:
        raise LookupError()


def _get_channel_list_v1(params, identifier=None):
    keyword = params.get("keyword", "").strip()
    language_id = params.get("language", "").strip()

    channels = None
    if identifier:
        channels = ChannelMetadata.objects.filter(pk=identifier)
    else:
        channels = ChannelMetadata.objects.all()

    if keyword != "":
        channels = channels.filter(
            Q(name__icontains=keyword) | Q(description__icontains=keyword)
        )

    if language_id != "":
        matching_tree_ids = (
            ContentNode.objects.prefetch_related("files")
            .filter(
                Q(lang__id__icontains=language_id)
                | Q(files__lang__id__icontains=language_id)
            )
            .values_list("tree_id", flat=True)
        )
        channels = channels.filter(
            Q(root__lang__id__icontains=language_id)
            | Q(root__tree_id__in=matching_tree_ids)
        )

    if not allow_peer_unlisted_channel_import():
        channels = channels.exclude(public=False)

    return channels.filter(root__available=True).distinct()


@api_view(["GET"])
def get_public_channel_list(request, version):
    """ Endpoint: /public/<version>/channels/?=<query params> """
    try:
        channel_list = _get_channel_list(version, request.query_params)
    except LookupError:
        return HttpResponseNotFound(
            json.dumps({"id": error_constants.NOT_FOUND, "metadata": {"view": ""}}),
            content_type="application/json",
        )
    return HttpResponse(
        json.dumps(PublicChannelSerializer(channel_list, many=True).data),
        content_type="application/json",
    )


@api_view(["GET"])
def get_public_channel_lookup(request, version, identifier):
    """ Endpoint: /public/<version>/channels/lookup/<identifier> """
    try:
        channel_list = _get_channel_list(
            version,
            request.query_params,
            identifier=identifier.strip().replace("-", ""),
        )
    except LookupError:
        return HttpResponseNotFound(
            json.dumps({"id": error_constants.NOT_FOUND, "metadata": {"view": ""}}),
            content_type="application/json",
        )

    if not channel_list.exists():
        return HttpResponseNotFound(
            json.dumps({"id": error_constants.NOT_FOUND, "metadata": {"view": ""}}),
            content_type="application/json",
        )
    return HttpResponse(
        json.dumps(PublicChannelSerializer(channel_list, many=True).data),
        content_type="application/json",
    )


@csrf_exempt
@gzip_page
def get_public_file_checksums(request, version):
    """ Endpoint: /public/<version>/file_checksums/ """
    if version == "v1":
        if request.content_type == "application/json":
            data = request.body
        elif request.content_type == "application/gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(request.body)) as f:
                data = f.read()
        else:
            return HttpResponseBadRequest("POST body must be either json or gzip")
        checksums = json.loads(data.decode("utf-8"))
        available_checksums = set(
            LocalFile.objects.filter(available=True)
            .filter_by_uuids(checksums)
            .values_list("id", flat=True)
            .distinct()
        )
        return HttpResponse(
            generate_checksum_integer_mask(checksums, available_checksums),
            content_type="application/octet-stream",
        )
    return HttpResponseNotFound(
        json.dumps({"id": error_constants.NOT_FOUND, "metadata": {"view": ""}}),
        content_type="application/json",
    )


class SyncQueueViewSet(viewsets.ViewSet):
    def list(self, request):
        """Returns length of the queue for each of the available facilities"""
        SyncQueue.clean_stale()  # first, ensure not expired devices are in the queue
        facilities = Facility.objects.all()
        queue = {}
        for facility in facilities:
            queue[facility.id] = SyncQueue.objects.filter(facility=facility).count()
        return Response(queue)

    def check_queue(self):
        current_transfers = TransferSession.objects.filter(active=True).count()
        if current_transfers <= MAX_CONCURRENT_SYNCS:
            allow_sync = True
            data = {"action": SYNC}
        else:
            polling = current_transfers + SyncQueue.objects.all().count()
            data = {
                "action": QUEUED,
                "keep_alive": polling,
            }
            allow_sync = False
        return (allow_sync, data)

    def create(self, request):
        SyncQueue.clean_stale()  # first, ensure not expired devices are in the queue

        device_info = get_device_info()
        if device_info["subset_of_users_device"]:
            content = {"I'm a Subset of users device": "Nothing to do here"}
            # would love to use HTTP 418, but it's not fully usable in browsers
            return Response(content, status=status.HTTP_404_NOT_FOUND)

        facility = request.data.get("facility")
        instance = request.data.get("instance")
        if facility is None or instance is None:
            content = {
                "Missing parameter(s)": "Both facility and instance are required"
            }
            return Response(content, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        if not Facility.objects.filter(id=facility).exists():
            content = {"This device is not registered in any of this server facilities"}
            return Response(content, status=status.HTTP_404_NOT_FOUND)

        allow_sync, data = self.check_queue()
        if not allow_sync:
            element, _ = SyncQueue.objects.get_or_create(
                facility_id=facility,
                instance_id=instance,
                keep_alive=data["keep_alive"],
            )
            data["key"] = element.key

        return Response(data)

    def update(self, request, pk=None):
        SyncQueue.clean_stale()  # first, ensure not expired devices are in the queue
        key = request.data.get("key") or pk
        if key is None:
            content = {"Missing parameter": "Key to update the queue is needed"}
            return Response(content, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        allow_sync, data = self.check_queue()

        if not allow_sync:
            element = SyncQueue.objects.filter(key=key).first()
            if not element:
                # this device has been deleted from the queue, likely due to keep alive expiration
                content = {
                    "Missing element": "This device is not registered in any of this server facilities"
                }
                return Response(content, status=status.HTTP_404_NOT_FOUND)
            element.keep_alive = data["keep_alive"]
            element.updated = time.time()
            element.save()
            data["key"] = element.key
        else:
            SyncQueue.objects.filter(key=key).delete()
        return Response(data)
