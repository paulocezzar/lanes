from django.urls import path

from . import api, views, views_people

urlpatterns = [
    path("", views.home, name="home"),
    path("my-cards/", views.my_cards, name="my_cards"),
    path("boards/new/", views.board_create, name="board_create"),
    path("b/<int:board_id>/", views.board_page, name="board"),
    path("cards/<int:card_id>/", views.card_modal, name="card_modal"),
    path("departments/new/", views_people.workspace_create, name="workspace_create"),
    path("departments/<int:workspace_id>/people/", views_people.workspace_members, name="workspace_members"),
    path("departments/<int:workspace_id>/people/add/", views_people.workspace_member_add, name="workspace_member_add"),
    path("departments/<int:workspace_id>/people/<int:user_id>/", views_people.workspace_member_update,
         name="workspace_member_update"),
    path("departments/<int:workspace_id>/people/<int:user_id>/reset-password/",
         views_people.workspace_member_reset_password, name="workspace_member_reset_password"),

    path("api/boards/<int:board_id>/lists/", api.list_create),
    path("api/boards/<int:board_id>/labels/", api.label_save),
    path("api/boards/<int:board_id>/share/", api.board_share),
    path("api/boards/<int:board_id>/members/<int:user_id>/", api.board_member_update),
    path("api/boards/<int:board_id>/join/", api.board_join),
    path("api/boards/<int:board_id>/star/", api.board_star),
    path("api/boards/<int:board_id>/update/", api.board_update),
    path("api/lists/<int:list_id>/", api.list_update),
    path("api/lists/<int:list_id>/move/", api.list_move),
    path("api/lists/<int:list_id>/cards/", api.card_create),
    path("api/cards/<int:card_id>/move/", api.card_move),
    path("api/cards/<int:card_id>/update/", api.card_update),
    path("api/cards/<int:card_id>/labels/", api.card_toggle_label),
    path("api/cards/<int:card_id>/members/", api.card_toggle_member),
    path("api/cards/<int:card_id>/checklists/", api.checklist_create),
    path("api/cards/<int:card_id>/comments/", api.comment_create),
    path("api/checklists/<int:checklist_id>/", api.checklist_update),
    path("api/checklists/<int:checklist_id>/items/", api.item_create),
    path("api/items/<int:item_id>/", api.item_update),
    path("api/comments/<int:comment_id>/", api.comment_update),
]
