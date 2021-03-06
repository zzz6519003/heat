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

from requests import exceptions

from heat.common import exception
from heat.common import template_format
from heat.common import urlfetch
from heat.engine.properties import Properties
from heat.engine import stack_resource

from heat.openstack.common import log as logging

logger = logging.getLogger(__name__)


(PROP_TEMPLATE_URL,
 PROP_TIMEOUT_MINS,
 PROP_PARAMETERS) = ('TemplateURL', 'TimeoutInMinutes', 'Parameters')


class NestedStack(stack_resource.StackResource):
    '''
    A Resource representing a child stack to allow composition of templates.
    '''

    properties_schema = {
        PROP_TEMPLATE_URL: {
            'Type': 'String',
            'Required': True,
            'UpdateAllowed': True,
            'Description': _('The URL of a template that specifies the stack'
                             ' to be created as a resource.')},
        PROP_TIMEOUT_MINS: {
            'Type': 'Number',
            'UpdateAllowed': True,
            'Description': _('The length of time, in minutes, to wait for the'
                             ' nested stack creation.')},
        PROP_PARAMETERS: {
            'Type': 'Map',
            'UpdateAllowed': True,
            'Description': _('The set of parameters passed to this nested'
                             ' stack.')}}

    update_allowed_keys = ('Properties',)

    def handle_create(self):
        try:
            template_data = urlfetch.get(self.properties[PROP_TEMPLATE_URL])
        except (exceptions.RequestException, IOError) as r_exc:
            raise ValueError("Could not fetch remote template '%s': %s" %
                             (self.properties[PROP_TEMPLATE_URL], str(r_exc)))

        template = template_format.parse(template_data)

        return self.create_with_template(template,
                                         self.properties[PROP_PARAMETERS],
                                         self.properties[PROP_TIMEOUT_MINS])

    def handle_delete(self):
        return self.delete_nested()

    def FnGetAtt(self, key):
        if key and not key.startswith('Outputs.'):
            raise exception.InvalidTemplateAttribute(resource=self.name,
                                                     key=key)
        return self.get_output(key.partition('.')[-1])

    def FnGetRefId(self):
        return self.nested().identifier().arn()

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        # Nested stack template may be changed even if the prop_diff is empty.
        self.properties = Properties(self.properties_schema,
                                     json_snippet.get('Properties', {}),
                                     self.stack.resolve_runtime_data,
                                     self.name)

        try:
            template_data = urlfetch.get(self.properties[PROP_TEMPLATE_URL])
        except (exceptions.RequestException, IOError) as r_exc:
            raise ValueError("Could not fetch remote template '%s': %s" %
                             (self.properties[PROP_TEMPLATE_URL], str(r_exc)))

        template = template_format.parse(template_data)

        return self.update_with_template(template,
                                         self.properties[PROP_PARAMETERS],
                                         self.properties[PROP_TIMEOUT_MINS])


def resource_mapping():
    return {
        'AWS::CloudFormation::Stack': NestedStack,
    }
