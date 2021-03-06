# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json

from heat.openstack.common import log as logging
from heat.openstack.common.importutils import try_import

from heat.common import exception
from heat.engine import clients
from heat.engine import resource
from heat.engine.resources import nova_utils
from heat.engine import scheduler

volume_backups = try_import('cinderclient.v1.volume_backups')

logger = logging.getLogger(__name__)


class Volume(resource.Resource):
    tags_schema = {'Key': {'Type': 'String',
                           'Required': True},
                   'Value': {'Type': 'String',
                             'Required': True}}

    properties_schema = {
        'AvailabilityZone': {
            'Type': 'String', 'Required': True,
            'Description': _('The availability zone in which the volume '
                             'will be created.')},
        'Size': {
            'Type': 'Number',
            'Description': _('The size of the volume in GB.')},
        'SnapshotId': {
            'Type': 'String',
            'Description': _('If specified, the backup used as the source '
                             'to create the volume.')},
        'Tags': {
            'Type': 'List',
            'Description': _('The list of tags to associate '
                             'with the volume.')},
            'Schema': {'Type': 'Map', 'Schema': tags_schema},
    }

    _restore_property = 'SnapshotId'

    _volume_creating_status = ['creating', 'restoring-backup']

    def _display_name(self):
        return self.physical_resource_name()

    def _display_description(self):
        return self.physical_resource_name()

    def _create_arguments(self):
        if self.properties['Tags']:
            tags = dict((tm['Key'], tm['Value'])
                        for tm in self.properties['Tags'])
        else:
            tags = None

        return {
            'size': self.properties['Size'],
            'availability_zone': self.properties['AvailabilityZone'] or None,
            'metadata': tags
        }

    def handle_create(self):
        backup_id = self.properties.get(self._restore_property)
        cinder = self.cinder()
        if backup_id is not None:
            if volume_backups is None:
                raise exception.Error(
                    '%s not supported' % self._restore_property)
            vol_id = cinder.restores.restore(backup_id).volume_id

            vol = cinder.volumes.get(vol_id)
            vol.update(
                display_name=self._display_name(),
                display_description=self._display_description())
        else:
            vol = cinder.volumes.create(
                display_name=self._display_name(),
                display_description=self._display_description(),
                **self._create_arguments())
        self.resource_id_set(vol.id)

        return vol

    def check_create_complete(self, vol):
        vol.get()

        if vol.status == 'available':
            return True
        elif vol.status in self._volume_creating_status:
            return False
        else:
            raise exception.Error(vol.status)

    def _backup(self):
        backup = self.cinder().backups.create(self.resource_id)
        while backup.status == 'creating':
            yield
            backup.get()
        if backup.status != 'available':
            raise exception.Error(backup.status)

    @scheduler.wrappertask
    def _delete(self, backup=False):
        if self.resource_id is not None:
            try:
                vol = self.cinder().volumes.get(self.resource_id)

                if backup:
                    yield self._backup()
                    vol.get()

                if vol.status == 'in-use':
                    logger.warn('cant delete volume when in-use')
                    raise exception.Error('Volume in use')

                vol.delete()
                while True:
                    yield
                    vol.get()
            except clients.cinderclient.exceptions.NotFound:
                self.resource_id_set(None)

    if volume_backups is not None:
        def handle_snapshot_delete(self, state):
            backup = state not in ((self.CREATE, self.FAILED),
                                   (self.UPDATE, self.FAILED))

            delete_task = scheduler.TaskRunner(self._delete, backup=backup)
            delete_task.start()
            return delete_task

    def handle_delete(self):
        delete_task = scheduler.TaskRunner(self._delete)
        delete_task.start()
        return delete_task

    def check_delete_complete(self, delete_task):
        return delete_task.step()


class VolumeAttachTask(object):
    """A task for attaching a volume to a Nova server."""

    def __init__(self, stack, server_id, volume_id, device):
        """
        Initialise with the stack (for obtaining the clients), ID of the
        server and volume, and the device name on the server.
        """
        self.clients = stack.clients
        self.server_id = server_id
        self.volume_id = volume_id
        self.device = device
        self.attachment_id = None

    def __str__(self):
        """Return a human-readable string description of the task."""
        return 'Attaching Volume %s to Instance %s as %s' % (self.volume_id,
                                                             self.server_id,
                                                             self.device)

    def __repr__(self):
        """Return a brief string description of the task."""
        return '%s(%s -> %s [%s])' % (type(self).__name__,
                                      self.volume_id,
                                      self.server_id,
                                      self.device)

    def __call__(self):
        """Return a co-routine which runs the task."""
        logger.debug(str(self))

        va = self.clients.nova().volumes.create_server_volume(
            server_id=self.server_id,
            volume_id=self.volume_id,
            device=self.device)
        self.attachment_id = va.id
        yield

        vol = self.clients.cinder().volumes.get(self.volume_id)
        while vol.status == 'available' or vol.status == 'attaching':
            logger.debug('%s - volume status: %s' % (str(self), vol.status))
            yield
            vol.get()

        if vol.status != 'in-use':
            raise exception.Error(vol.status)

        logger.info('%s - complete' % str(self))


class VolumeDetachTask(object):
    """A task for detaching a volume from a Nova server."""

    def __init__(self, stack, server_id, volume_id):
        """
        Initialise with the stack (for obtaining the clients), and the IDs of
        the server and volume.
        """
        self.clients = stack.clients
        self.server_id = server_id
        self.volume_id = volume_id

    def __str__(self):
        """Return a human-readable string description of the task."""
        return 'Detaching Volume %s from Instance %s' % (self.volume_id,
                                                         self.server_id)

    def __repr__(self):
        """Return a brief string description of the task."""
        return '%s(%s -/> %s)' % (type(self).__name__,
                                  self.volume_id,
                                  self.server_id)

    def __call__(self):
        """Return a co-routine which runs the task."""
        logger.debug(str(self))

        try:
            vol = self.clients.cinder().volumes.get(self.volume_id)
        except clients.cinderclient.exceptions.NotFound:
            logger.warning('%s - volume not found' % str(self))
            return

        server_api = self.clients.nova().volumes

        try:
            server_api.delete_server_volume(self.server_id, self.volume_id)
        except (clients.novaclient.exceptions.BadRequest,
                clients.novaclient.exceptions.NotFound) as e:
            logger.warning('%s - %s' % (str(self), str(e)))

        yield

        try:
            vol.get()
            while vol.status in ('in-use', 'detaching'):
                logger.debug('%s - volume still in use' % str(self))
                yield

                try:
                    server_api.delete_server_volume(self.server_id,
                                                    self.volume_id)
                except (clients.novaclient.exceptions.BadRequest,
                        clients.novaclient.exceptions.NotFound):
                    pass
                vol.get()

            logger.info('%s - status: %s' % (str(self), vol.status))
            if vol.status != 'available':
                raise exception.Error(vol.status)

        except clients.cinderclient.exceptions.NotFound:
            logger.warning('%s - volume not found' % str(self))


class VolumeAttachment(resource.Resource):
    properties_schema = {
        'InstanceId': {
            'Type': 'String', 'Required': True,
            'Description': _('The ID of the instance to which the '
                             'volume attaches.')},
        'VolumeId': {
            'Type': 'String', 'Required': True,
            'Description': _('The ID of the volume to be attached.')},
        'Device': {
            'Type': 'String', 'Required': True,
            'AllowedPattern': '/dev/vd[b-z]',
            'Description': _('The device where the volume is exposed on '
                             'the instance.')}
    }

    _instance_property = 'InstanceId'
    _volume_property = 'VolumeId'
    _device_property = 'Device'

    def handle_create(self):
        server_id = self.properties[self._instance_property]
        volume_id = self.properties[self._volume_property]
        dev = self.properties[self._device_property]

        attach_task = VolumeAttachTask(self.stack, server_id, volume_id, dev)
        attach_runner = scheduler.TaskRunner(attach_task)

        attach_runner.start()

        self.resource_id_set(attach_task.attachment_id)

        return attach_runner

    def check_create_complete(self, attach_runner):
        return attach_runner.step()

    def handle_delete(self):
        server_id = self.properties[self._instance_property]
        volume_id = self.properties[self._volume_property]
        detach_task = VolumeDetachTask(self.stack, server_id, volume_id)
        scheduler.TaskRunner(detach_task)()


class CinderVolume(Volume):

    properties_schema = {
        'availability_zone': {
            'Type': 'String',
            'Description': _('The availability zone in which the volume '
                             'will be created.')},
        'size': {
            'Type': 'Number',
            'Description': _('The size of the volume in GB.')},
        'snapshot_id': {
            'Type': 'String',
            'Description': _('If specified, the snapshot to create the '
                             'volume from.')},
        'backup_id': {
            'Type': 'String',
            'Description': _('If specified, the backup to create the '
                             'volume from.')},
        'name': {
            'Type': 'String',
            'Description': _('A name used to distinguish the volume.')},
        'description': {
            'Type': 'String',
            'Description': _('A description of the volume.')},
        'volume_type': {
            'Type': 'String',
            'Description': _('If specified, the type of volume to use, '
                             'mapping to a specific backend.')},
        'metadata': {
            'Type': 'Map',
            'Description': _('Key/value pairs to associate with the volume.')},
        'imageRef': {
            'Type': 'String',
            'Description': _('DEPRECATED: use "image" instead.')},
        'image': {
            'Type': 'String',
            'Description': _('If specified, the name or ID of the image to '
                             'create the volume from.')},
        'source_volid': {
            'Type': 'String',
            'Description': _('If specified, the volume to use as source.')}
    }

    attributes_schema = {
        'availability_zone': _('The availability zone in which the volume is '
                               ' located.'),
        'size': _('The size of the volume in GB.'),
        'snapshot_id': _('The snapshot the volume was created from, if any.'),
        'display_name': _('Name of the volume.'),
        'display_description': _('Description of the volume.'),
        'volume_type': _('The type of the volume mapping to a backend, if '
                         'any.'),
        'metadata': _('Key/value pairs associated with the volume.'),
        'source_volid': _('The volume used as source, if any.'),
        'status': _('The current status of the volume.'),
        'created_at': _('The timestamp indicating volume creation.'),
        'bootable': _('Boolean indicating if the volume can be booted or '
                      'not.'),
    }

    _restore_property = 'backup_id'

    _volume_creating_status = ['creating', 'restoring-backup', 'downloading']

    def _display_name(self):
        name = self.properties['name']
        if name:
            return name
        return super(CinderVolume, self)._display_name()

    def _display_description(self):
        return self.properties['description']

    def _create_arguments(self):
        arguments = {
            'size': self.properties['size'],
            'availability_zone': self.properties['availability_zone']
        }
        if self.properties.get('image'):
            arguments['imageRef'] = nova_utils.get_image_id(
                self.nova(), self.properties['image'])
        elif self.properties.get('imageRef'):
            arguments['imageRef'] = self.properties['imageRef']

        optionals = ['snapshot_id', 'volume_type', 'source_volid',
                     'metadata']
        arguments.update((prop, self.properties[prop]) for prop in optionals
                         if self.properties[prop])
        return arguments

    def _resolve_attribute(self, name):
        vol = self.cinder().volumes.get(self.resource_id)
        if name == 'metadata':
            return unicode(json.dumps(vol.metadata))
        return unicode(getattr(vol, name))


class CinderVolumeAttachment(VolumeAttachment):

    properties_schema = {
        'instance_uuid': {
            'Type': 'String', 'Required': True,
            'Description': _('The ID of the server to which the '
                             'volume attaches.')},
        'volume_id': {
            'Type': 'String', 'Required': True,
            'Description': _('The ID of the volume to be attached.')},
        'mountpoint': {
            'Type': 'String', 'Required': True,
            'Description': _('The location where the volume is exposed on '
                             'the instance.')}
    }

    _instance_property = 'instance_uuid'
    _volume_property = 'volume_id'
    _device_property = 'mountpoint'


def resource_mapping():
    return {
        'AWS::EC2::Volume': Volume,
        'AWS::EC2::VolumeAttachment': VolumeAttachment,
        'OS::Cinder::Volume': CinderVolume,
        'OS::Cinder::VolumeAttachment': CinderVolumeAttachment,
    }
