{
    'name': 'Runbot Jobs',
    'category': 'Website',
    'summary': 'Runbot Jobs',
    'version': '2.0',
    'description': "Runbot Jobs",
    'author': 'Odoo SA',
    'depends': ['runbot'],
    'data': [
        'data/runbot.job.csv',
        'views/repo.xml',
        'views/job.xml',
        ],
}
