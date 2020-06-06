#
# Copyright (C) 2019 by frePPLe bv
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [("input", "0041_buffer_remove_name")]

    operations = [
        migrations.AddField(
            model_name="operation",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="childoperations",
                to="input.Operation",
                verbose_name="owner",
            ),
        ),
        migrations.RunSQL(
            """
            update operation
            set priority = suboperation.priority,
                owner_id = suboperation.operation_id,
                effective_start = suboperation.effective_start,
                effective_end = suboperation.effective_end
            from suboperation
            where suboperation.suboperation_id = operation.name
            """
        ),
    ]
