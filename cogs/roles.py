import discord
import logging
import emoji

from discord.ext import commands, tasks
from asyncpg.exceptions import UniqueViolationError

from cogs.mixins import AceMixin
from utils.checks import is_mod_pred
from utils.pager import Pager
from utils.configtable import ConfigTable

# TODO: role add rate limiting?

VALID_FIELDS = dict(
	emoji=8,
	name=248,
	description=1024
)


class RolePager(Pager):
	async def craft_page(self, e, page, entries):
		for role in entries:
			e.add_field(
				name=role.name,
				value='ID: {}'.format(str(role.id))
			)

		e.set_author(
			name=self.ctx.guild.name,
			icon_url=self.ctx.guild.icon_url
		)


class EmojiConverter(commands.Converter):
	async def convert(self, ctx, emoj):
		if emoj not in emoji.UNICODE_EMOJI:
			raise commands.CommandError('Unknown emoji.')
		return emoj


class RoleIDConverter(commands.Converter):
	async def convert(self, ctx, id):
		try:
			role = await commands.RoleConverter().convert(ctx, id)
			return role.id
		except commands.BadArgument:
			try:
				ret = int(id)
				return ret
			except ValueError:
				raise commands.CommandError('Input has to be a role or an integer.')


class Roles(AceMixin, commands.Cog):
	'''Create a role selection menu.'''

	def __init__(self, bot):
		super().__init__(bot)

		self.config = ConfigTable(bot, table='role', primary='guild_id')
		self.setup_configs.start()

	# init configs
	@tasks.loop(count=1)
	async def setup_configs(self):
		records = await self.db.fetch('SELECT * FROM {}'.format(self.config.table))

		for record in records:
			await self.config.insert_record(record)

	async def cog_check(self, ctx):
		return await is_mod_pred(ctx)

	@commands.group(hidden=True)
	async def roles(self, ctx):
		pass

	@roles.command()
	async def spawn(self, ctx):
		'''Spawns the role selector. Deletes previous role selector instance.'''

		conf = await self.config.get_entry(ctx.guild.id)
		if not len(conf.roles):
			raise commands.CommandError('No roles configured.')

		e = discord.Embed(
			description='Click the reactions to give or remove roles.'
		)

		e.set_author(
			name='{} Roles'.format(ctx.guild.name),
			icon_url=ctx.guild.icon_url
		)

		guild_roles = await self.db.fetch('SELECT * FROM role_entry WHERE id = ANY($1::INTEGER[])', conf.roles)
		emojis = list()

		# this thing could probably be improved
		for role_entry_id in conf.roles:
			for role in filter(lambda role: role.get('id') == role_entry_id, guild_roles):
				emoji = role.get('emoji')

				e.add_field(
					name='{} {}'.format(emoji, role.get('name')),
					value=role.get('description'),
					inline=conf.inline
				)

				emojis.append(emoji)
				break
			else:
				raise commands.CommandError('Database dirty. Please contact bot owner.')

		# delete old message
		if conf.message_id is not None:
			old_channel = ctx.guild.get_channel(conf.channel_id)
			if old_channel is not None:
				try:
					old_message = await old_channel.fetch_message(conf.message_id)
					await old_message.delete()
				except discord.HTTPException:
					pass

		try:
			await ctx.message.delete()
		except discord.HTTPException:
			pass

		msg = await ctx.send(embed=e)

		for emoji in emojis:
			await msg.add_reaction(emoji)

		await conf.update(channel_id=msg.channel.id, message_id=msg.id)

	@roles.command()
	async def add(self, ctx, role: discord.Role, emoji: EmojiConverter, name: str, *, description: str):
		'''Add a new role to the role selector. To add non-mentionable roles, get their ID using `roles all`.'''

		if len(description) < 1 or len(description) > 1024:
			raise commands.CommandError('Description has to be between 1 and 1024 characters long.')

		if len(name) < 1 or len(name) > 248:
			raise commands.CommandError('Name has to be between 1 and 250 characters long.')

		gc = await self.bot.config.get_entry(ctx.guild.id)
		if role.id == gc.mod_role_id:
			raise commands.CommandError('Moderator/mute role can\'t be added to the roles selector.')

		try:
			id = await self.db.fetchval(
				'INSERT INTO role_entry (role_id, emoji, name, description) VALUES ($1, $2, $3, $4) RETURNING id',
				role.id, str(emoji), name, description
			)
		except UniqueViolationError:
			raise commands.CommandError('Role already added.')

		conf = await self.config.get_entry(ctx.guild.id)

		conf.roles.append(id)
		conf._set_dirty('roles')
		await conf.update()

		await ctx.send('Role added. Do `roles spawn` to create new role selector menu.')

	@roles.command()
	async def remove(self, ctx, role: RoleIDConverter):
		'''Remove a role from the role selector.'''

		conf = await self.config.get_entry(ctx.guild.id)

		role_row = await self.db.fetchrow(
			'SELECT * FROM role_entry WHERE role_id=$1 AND id = ANY($2::INTEGER[])',
			role, conf.roles
		)

		if role_row is None:
			raise commands.CommandError('Role not in the role selector.')

		await self.db.execute('DELETE FROM role_entry WHERE id=$1', role_row.get('id'))

		conf.roles.remove(role_row.get('id'))
		conf._set_dirty('roles')
		await conf.update()

		await ctx.send('Role removed from the role selector.')

	@roles.command()
	async def edit(self, ctx, role: RoleIDConverter, field: str, *, new_value: str):
		'''Edit a field of a role field. Valid fields are `emoji`, `name` and `description`.'''

		field = field.lower()

		if field not in VALID_FIELDS:
			raise commands.CommandError('Sorry, \'{}\' is not a valid field.'.format(field))

		# emoji has to run through converter
		if field == 'emoji':
			new_value = await EmojiConverter().convert(ctx, emoj=new_value)

		char_limit = VALID_FIELDS[field]

		if len(field) > char_limit:
			raise commands.CommandError(
				'New value is too long, maximum length for this field is {} characters.'.format(char_limit)
			)

		conf = await self.config.get_entry(ctx.guild.id)
		role_db_id = await self.db.fetchval('SELECT id FROM role_entry WHERE role_id=$1', role)

		if role_db_id is None or role_db_id not in conf.roles:
			raise commands.CommandError('Role with id {} is not set up in the role selector yet.'.format(role))

		await self.db.execute('UPDATE role_entry SET {}=$1 WHERE id=$2'.format(field), new_value, role_db_id)
		await ctx.send('Value updated. Respawn role selector for updated version.')

	@roles.command()
	async def inline(self, ctx):
		'''Toggle whether embed fields in the role selector should be inline.'''

		conf = await self.config.get_entry(ctx.guild.id)

		await conf.update(inline=not conf.inline)

		await ctx.send('Inline fields {}.'.format('enabled' if conf.inline else 'disabled'))

	@roles.command()
	async def moveup(self, ctx, role: RoleIDConverter):
		'''Move a role up in the Role Selector.'''

		await self._moverole(ctx, role, -1)
		await ctx.send('Role moved up.')

	@roles.command()
	async def movedown(self, ctx, role: RoleIDConverter):
		'''Move a role down in the Role Selector'''

		await self._moverole(ctx, role, 1)
		await ctx.send('Role moved down.')

	async def _moverole(self, ctx, role, direction):
		conf = await self.config.get_entry(ctx.guild.id)

		role_row = await self.db.fetchrow(
			'SELECT * FROM role_entry WHERE role_id=$1 AND id = ANY($2::INTEGER[])',
			role, conf.roles
		)

		if role_row is None:
			raise commands.CommandError('Role not registered.')

		ids = conf.roles

		if len(ids) == 1:
			raise commands.CommandError('Role is the only role registered.')

		pos = None

		for idx, id in enumerate(ids):
			if id == role_row.get('id'):
				pos = idx
				break

		if pos is None:
			raise commands.CommandError('Role not registered.')

		if pos == 0 and direction == -1:
			raise commands.CommandError('Role is already first.')

		if pos == len(ids) - 1 and direction == 1:
			raise commands.CommandError('Role is already last.')

		# actually do the swap
		ids[pos], ids[pos + direction] = ids[pos + direction], ids[pos]

		await conf.update(roles=ids)

	@roles.command()
	async def print(self, ctx):
		'''Print information about the roles currently in the Role Selector.'''

		conf = await self.config.get_entry(ctx.guild.id)

		role_rows = await self.db.fetch(
			'SELECT * FROM role_entry WHERE id = ANY($1::INTEGER[])',
			conf.roles
		)

		if not role_rows:
			raise commands.CommandError('No roles registered or found.')

		e = discord.Embed()

		for role_id in conf.roles:
			for role in filter(lambda role: role.get('id') == role_id, role_rows):
				e.add_field(
					name=role.get('name'),
					value='ROLE ID: {}\nEMOJI: {}'.format(role.get('role_id'), role.get('emoji'))
				)

		await ctx.send(embed=e)

	@roles.command(name='list', aliases=['all'])
	async def _list(self, ctx):
		'''List all roles in this server.'''

		p = RolePager(ctx, list(reversed(ctx.guild.roles[1:])), per_page=24)
		await p.go()

	@commands.Cog.listener()
	async def on_raw_reaction_add(self, payload):
		conf = await self.config.get_entry(payload.guild_id, construct=False)

		if conf is None:
			return

		if conf.channel_id != payload.channel_id:
			return

		if conf.message_id != payload.message_id:
			return

		guild = self.bot.get_guild(payload.guild_id)
		if guild is None:
			return

		member = guild.get_member(payload.user_id)
		if member is None or member.bot:
			return

		channel = guild.get_channel(payload.channel_id)
		if channel is None:
			return

		message = await channel.fetch_message(payload.message_id)
		if message is None:
			return

		await message.remove_reaction(payload.emoji, member)

		role_row = await self.db.fetchrow(
			'SELECT * FROM role_entry WHERE emoji=$1 AND id = ANY($2::INTEGER[])',
			str(payload.emoji), conf.roles
		)

		if role_row is None:
			return

		role = guild.get_role(role_row.get('role_id'))
		if role is None:
			await channel.send(
				f'Role with ID `{role_row.get("role_id")}` registered but not found. Has it been deleted?',
				delete_after=10
			)
			return

		try:
			if role in member.roles:
				await member.remove_roles(role, reason='Removed through Role Selector')
				added = False
			else:
				await member.add_roles(role, reason='Added through Role Selector')
				added = True
		except discord.Forbidden:
			await channel.send('Sorry, I\'m not allow to manage roles.', delete_after=10)
			return
		except discord.HTTPException:
			await channel.send('Sorry, something went wrong.', delete_after=10)
			return

		e = discord.Embed(
			title='Role {}'.format('Added' if added else 'Removed'),
			description=role.mention
		)

		e.set_author(name=member.display_name, icon_url=member.avatar_url)

		await channel.send(embed=e, delete_after=10)


def setup(bot):
	bot.add_cog(Roles(bot))
