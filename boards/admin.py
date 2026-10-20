from django.contrib import admin

from . import models


class WorkspaceMemberInline(admin.TabularInline):
    model = models.WorkspaceMember
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(models.Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]
    inlines = [WorkspaceMemberInline]


class BoardMemberInline(admin.TabularInline):
    model = models.BoardMember
    extra = 1
    autocomplete_fields = ["user"]


@admin.register(models.Board)
class BoardAdmin(admin.ModelAdmin):
    list_display = ["title", "workspace", "visibility", "is_archived", "created_at"]
    list_filter = ["workspace", "visibility", "is_archived"]
    search_fields = ["title"]
    inlines = [BoardMemberInline]


@admin.register(models.Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ["title", "list", "due_at", "is_complete", "is_archived"]
    list_filter = ["is_complete", "is_archived", "list__board__workspace"]
    search_fields = ["title", "description"]


@admin.register(models.Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ["created_at", "board", "card", "actor", "verb"]
    list_filter = ["verb"]
    readonly_fields = [f.name for f in models.Activity._meta.fields]
